import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from .allocation import allocate
from .errors import APIError

# 咨询锁：使用单 bigint 键，高 32 位为服务命名空间、低 32 位为期间键
ADVISORY_NS = 0x524F5941  # "ROYA"

# 分页上限
MAX_PAGE_SIZE = 200


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


# 当期"有效播放"：仅更正链链尾参与核算；LEFT JOIN 按发生时间匹配协议
EFFECTIVE_PLAYS_SQL = """
SELECT
    p.import_id,
    p.work_code,
    p.occurred_at,
    p.amount,
    a.id AS agreement_id
FROM plays p
LEFT JOIN agreements a
    ON a.work_code = p.work_code
   AND tstzrange(a.valid_from, a.valid_to, '[)') @> p.occurred_at
WHERE p.occurred_at >= %(start)s
  AND p.occurred_at <  %(end)s
  AND NOT EXISTS (
      SELECT 1 FROM plays c WHERE c.corrects_id = p.import_id
  )
ORDER BY p.occurred_at, p.import_id
"""

# 为一批链尾解析更正链"原始编号"：沿 corrects_id 回溯。
# plays.corrects_id 唯一 + 禁止自引用保证链线性无环。
ROOTS_SQL = """
WITH RECURSIVE chain AS (
    SELECT import_id AS tail_id, import_id AS node_id, corrects_id, 0 AS depth
    FROM plays
    WHERE import_id = ANY(%(tail_ids)s)
    UNION ALL
    SELECT ch.tail_id, p.import_id, p.corrects_id, ch.depth + 1
    FROM plays p
    JOIN chain ch ON ch.corrects_id = p.import_id
)
SELECT tail_id, node_id AS root_id
FROM chain
WHERE corrects_id IS NULL
"""


def canonical_period(start: datetime, end: datetime) -> tuple[datetime, datetime, str]:
    """统一按 UTC 存储/比较期间，返回 (start_utc, end_utc, key)。"""
    s = start.astimezone(timezone.utc)
    e = end.astimezone(timezone.utc)
    return s, e, f"{s.isoformat()}|{e.isoformat()}"


def _period_lock_key(key: str) -> int:
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    # 转成 int8 有符号数
    return n - (1 << 64) if n >= (1 << 63) else n


def _pending_payload(rows) -> list[dict]:
    return [
        {
            "import_id": r["import_id"],
            "work_code": r["work_code"],
            "occurred_at": r["occurred_at"],
            "amount": r["amount"],
            "reason": r["reason"],
        }
        for r in rows
    ]


def compute_current_detailed(conn, start: datetime, end: datetime):
    """按当前最新数据（后补协议、更正后的链尾）重算期间。

    返回 (plays_detail, pending)：
      plays_detail: 参与核算的逐笔播放列表（按更正链原始编号归集），每项含
          root/tail/work_code/occurred_at/agreement_id 与逐笔分摊 allocations；
      pending: 待处理播放（缺协议，不计入应付）。
    待处理播放不计入应付。
    """
    rows = conn.execute(
        EFFECTIVE_PLAYS_SQL, {"start": start, "end": end}
    ).fetchall()

    # 一次性解析所有链尾的原始编号
    roots: dict[str, str] = {}
    if rows:
        root_rows = conn.execute(
            ROOTS_SQL, {"tail_ids": [r["import_id"] for r in rows]}
        ).fetchall()
        roots = {rr["tail_id"]: rr["root_id"] for rr in root_rows}

    agreement_ids = {r["agreement_id"] for r in rows if r["agreement_id"] is not None}
    shares_map: dict[int, dict[str, Decimal]] = {}
    if agreement_ids:
        share_rows = conn.execute(
            """
            SELECT agreement_id, holder_code, percent
            FROM agreement_shares
            WHERE agreement_id = ANY(%s)
            ORDER BY holder_code
            """,
            (list(agreement_ids),),
        ).fetchall()
        for sr in share_rows:
            shares_map.setdefault(sr["agreement_id"], {})[sr["holder_code"]] = sr["percent"]

    plays_detail: list[dict] = []
    pending: list[dict] = []
    for r in rows:
        if r["agreement_id"] is None:
            pending.append(
                {
                    "import_id": r["import_id"],
                    "work_code": r["work_code"],
                    "occurred_at": r["occurred_at"],
                    "amount": r["amount"],
                    "reason": "no matching agreement at play occurrence time",
                }
            )
            continue
        # 逐笔按比例分到最小货币单位，余数按规则补足
        allocations = allocate(r["amount"], shares_map[r["agreement_id"]])
        plays_detail.append(
            {
                "root_import_id": roots.get(r["import_id"], r["import_id"]),
                "tail_import_id": r["import_id"],
                "work_code": r["work_code"],
                "occurred_at": r["occurred_at"],
                "agreement_id": r["agreement_id"],
                "amount": r["amount"],
                "allocations": allocations,
            }
        )

    pending.sort(key=lambda x: (x["occurred_at"], x["import_id"]))
    # 证据顺序按原始编号归集后稳定排序
    plays_detail.sort(
        key=lambda d: (d["occurred_at"], d["root_import_id"], d["tail_import_id"])
    )
    return plays_detail, pending


def compute_current(conn, start: datetime, end: datetime):
    """兼容旧调用：返回 (payable 汇总, pending)。"""
    plays_detail, pending = compute_current_detailed(conn, start, end)
    payable: dict[str, int] = {}
    for d in plays_detail:
        for holder, amt in d["allocations"].items():
            payable[holder] = payable.get(holder, 0) + amt
    return payable, pending


def _load_pending_snapshot(conn, start: datetime, end: datetime):
    rows = conn.execute(
        """
        SELECT import_id, work_code, occurred_at, amount, reason
        FROM statement_pending
        WHERE period_start = %s AND period_end = %s
        ORDER BY occurred_at, import_id
        """,
        (start, end),
    ).fetchall()
    return _pending_payload(rows)


def _load_prior_payable(conn, start: datetime, end: datetime) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT l.holder_code, COALESCE(SUM(l.amount), 0) AS total
        FROM statement_lines l
        JOIN statement_batches b ON b.id = l.batch_id
        WHERE b.period_start = %s AND b.period_end = %s
        GROUP BY l.holder_code
        """,
        (start, end),
    ).fetchall()
    return {r["holder_code"]: r["total"] for r in rows}


def _insert_pending_snapshot(conn, start: datetime, end: datetime, pending: list[dict]):
    conn.execute(
        "DELETE FROM statement_pending WHERE period_start = %s AND period_end = %s",
        (start, end),
    )
    if pending:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO statement_pending
                    (period_start, period_end, import_id, work_code, occurred_at, amount, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (start, end, p["import_id"], p["work_code"], p["occurred_at"],
                     p["amount"], p["reason"])
                    for p in pending
                ],
            )


def _latest_batch(conn, start: datetime, end: datetime):
    return conn.execute(
        """
        SELECT id, kind, supersedes_batch_id, created_at, traceable, attribution_mode
        FROM statement_batches
        WHERE period_start = %s AND period_end = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (start, end),
    ).fetchone()


def _has_untraceable_batch(conn, start: datetime, end: datetime) -> bool:
    """期间是否存在无逐笔证据、且尚未被锚定过的存量旧单。

    功能上线前已存在的单据没有逐笔证据；旧账无法归因的基线差额
    只在其后第一张追补单(legacy_anchored)中单列一次，之后回归普通 delta。
    """
    row = conn.execute(
        """
        SELECT 1
        FROM statement_batches b
        WHERE b.period_start = %s AND b.period_end = %s
          AND b.traceable = false
          AND NOT EXISTS (
              SELECT 1 FROM statement_batches a
              WHERE a.period_start = b.period_start
                AND a.period_end = b.period_end
                AND a.attribution_mode = 'legacy_anchored'
          )
        LIMIT 1
        """,
        (start, end),
    ).fetchone()
    return row is not None


def _load_evidenced_payable(conn, start: datetime, end: datetime):
    """此前各可追溯单据归因证据累计的逐链尾/逐权利人金额。

    只汇总 traceable 单据；存量旧单之后的首张可追溯追补单采用
    legacy_anchored 模式，不与旧账混算逐笔差额。
    返回 (totals, root_by_tail)。
    """
    rows = conn.execute(
        """
        SELECT e.tail_import_id, e.chain_root_id, e.holder_code,
               COALESCE(SUM(e.amount), 0) AS total
        FROM statement_attribution_entries e
        JOIN statement_batches b ON b.id = e.batch_id
        WHERE b.period_start = %s AND b.period_end = %s
          AND b.traceable = true
          AND e.entry_type = 'play'
        GROUP BY e.tail_import_id, e.chain_root_id, e.holder_code
        """,
        (start, end),
    ).fetchall()
    totals: dict[tuple[str, str], int] = {}
    root_by_tail: dict[str, str] = {}
    for r in rows:
        key = (r["tail_import_id"], r["holder_code"])
        totals[key] = totals.get(key, 0) + r["total"]
        if r["tail_import_id"] is not None and r["chain_root_id"] is not None:
            root_by_tail.setdefault(r["tail_import_id"], r["chain_root_id"])
    return totals, root_by_tail


def _insert_batch(
    conn, start, end, kind, supersedes, lines: dict[str, int],
    traceable: bool = True, attribution_mode: str = "delta",
):
    row = conn.execute(
        """
        INSERT INTO statement_batches(
            period_start, period_end, kind, supersedes_batch_id,
            traceable, attribution_mode)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (start, end, kind, supersedes, traceable, attribution_mode),
    ).fetchone()
    if lines:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO statement_lines(batch_id, holder_code, amount) VALUES (%s, %s, %s)",
                [(row["id"], holder, amt) for holder, amt in lines.items()],
            )
    return row["id"], row["created_at"]


def _insert_entries(conn, batch_id: int, entries: list[dict]):
    """冻结本单据的逐笔归因证据（含旧账基线行）。"""
    if not entries:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO statement_attribution_entries(
                batch_id, holder_code, chain_root_id, tail_import_id,
                work_code, occurred_at, agreement_id,
                entry_type, change_type, amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    batch_id,
                    e["holder_code"],
                    e.get("chain_root_id"),
                    e.get("tail_import_id"),
                    e.get("work_code"),
                    e.get("occurred_at"),
                    e.get("agreement_id"),
                    e["entry_type"],
                    e["change_type"],
                    e["amount"],
                )
                for e in entries
            ],
        )


def _build_entries_for_detail(detail: list[dict], change: str) -> list[dict]:
    """把逐笔播放明细展开为归因证据行（initial/added 全量，非负）。"""
    entries = []
    for d in detail:
        for holder in sorted(d["allocations"]):
            entries.append(
                {
                    "holder_code": holder,
                    "chain_root_id": d["root_import_id"],
                    "tail_import_id": d["tail_import_id"],
                    "work_code": d["work_code"],
                    "occurred_at": d["occurred_at"],
                    "agreement_id": d["agreement_id"],
                    "entry_type": "play",
                    "change_type": change,
                    "amount": d["allocations"][holder],
                }
            )
    return entries


def _build_delta_entries(
    detail: list[dict],
    prior_by_tail: dict[tuple[str, str], int],
    root_by_tail: dict[str, str],
) -> list[dict]:
    """delta 模式：相对上次可追溯出具结果计算逐笔差额。

    跨期更正（链尾编号变化）：旧 tail 消失 => 撤销行（负额），
    新 tail 出现 => 新增行（正额）；协议后补 => 原 pending 播放新增。
    负额照存，绝不丢失。
    """
    entries = []
    seen: set[tuple[str, str]] = set()
    for d in detail:
        for holder in sorted(d["allocations"]):
            cur_amt = d["allocations"][holder]
            old_amt = prior_by_tail.get((d["tail_import_id"], holder), 0)
            seen.add((d["tail_import_id"], holder))
            delta = cur_amt - old_amt
            if delta == 0:
                continue
            entries.append(
                {
                    "holder_code": holder,
                    "chain_root_id": d["root_import_id"],
                    "tail_import_id": d["tail_import_id"],
                    "work_code": d["work_code"],
                    "occurred_at": d["occurred_at"],
                    "agreement_id": d["agreement_id"],
                    "entry_type": "play",
                    "change_type": "added" if delta > 0 else "revoked",
                    "amount": delta,
                }
            )
    # 当前不再参与核算的旧链尾：逐笔撤销（负额），旧账仍可辨
    for (tail_id, holder), old_amt in prior_by_tail.items():
        if old_amt != 0 and (tail_id, holder) not in seen:
            entries.append(
                {
                    "holder_code": holder,
                    "chain_root_id": root_by_tail.get(tail_id),
                    "tail_import_id": tail_id,
                    "work_code": None,
                    "occurred_at": None,
                    "agreement_id": None,
                    "entry_type": "play",
                    "change_type": "revoked",
                    "amount": -old_amt,
                }
            )
    return entries


def _build_legacy_baseline_entries(prior_payable: dict[str, int]) -> list[dict]:
    """旧账无法归因的差额单列：以前累计额取反为基线，不冒充当前播放。"""
    return [
        {
            "holder_code": holder,
            "chain_root_id": None,
            "tail_import_id": None,
            "work_code": None,
            "occurred_at": None,
            "agreement_id": None,
            "entry_type": "legacy_baseline",
            "change_type": "legacy_baseline",
            "amount": -prior_payable.get(holder, 0),
        }
        for holder in sorted(prior_payable)
        if prior_payable.get(holder, 0) != 0
    ]


def _sum_entries_by_holder(entries: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for e in entries:
        totals[e["holder_code"]] = totals.get(e["holder_code"], 0) + e["amount"]
    return totals


def _build_response(batch, start, end, payable, prior, pending, note=None):
    if batch["kind"] == "initial":
        lines = [
            {"holder_code": h, "amount": payable.get(h, 0), "delta": None}
            for h in sorted(payable)
            if payable.get(h, 0) != 0
        ]
    else:
        # 追补单列出本期全部权利人当前应付；差额为 0 也保留行，delta 置空
        holders = set(payable) | set(prior)
        lines = []
        for h in sorted(holders):
            delta = payable.get(h, 0) - prior.get(h, 0)
            lines.append(
                {
                    "holder_code": h,
                    "amount": payable.get(h, 0),
                    "delta": delta if delta != 0 else None,
                }
            )
    return {
        "batch_id": batch["id"],
        "kind": batch["kind"],
        "supersedes_batch_id": batch["supersedes_batch_id"],
        "period_start": start,
        "period_end": end,
        "issued_at": batch["created_at"],
        "note": note,
        "traceable": batch.get("traceable", True),
        "attribution_mode": batch.get("attribution_mode", "delta"),
        "payable": lines,
        "pending": pending,
    }


def issue_statement(conn, start_in: datetime, end_in: datetime):
    start, end, key = canonical_period(start_in, end_in)
    with conn.transaction():
        # 同一期间串行化出具，杜绝并发重复单据与重复证据
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(ADVISORY_NS, _period_lock_key(key)),),
        )

        detail, pending = compute_current_detailed(conn, start, end)
        payable = _sum_entries_by_holder(
            [
                {"holder_code": h, "amount": amt}
                for d in detail for h, amt in d["allocations"].items()
            ]
        )
        _insert_pending_snapshot(conn, start, end, pending)

        existing_initial = conn.execute(
            "SELECT id FROM statement_batches WHERE period_start = %s AND period_end = %s AND kind = 'initial'",
            (start, end),
        ).fetchone()

        if existing_initial is None:
            lines = {h: amt for h, amt in payable.items() if amt != 0}
            entries = _build_entries_for_detail(detail, "initial")
            batch_id, created_at = _insert_batch(
                conn, start, end, "initial", None, lines,
                traceable=True, attribution_mode="delta",
            )
            _insert_entries(conn, batch_id, entries)
            batch = {
                "id": batch_id,
                "kind": "initial",
                "supersedes_batch_id": None,
                "created_at": created_at,
                "traceable": True,
                "attribution_mode": "delta",
            }
            return _build_response(batch, start, end, payable, {}, pending)

        # 此前全部单据（含可能的存量旧单）累计应付
        prior = _load_prior_payable(conn, start, end)
        holder_deltas = {
            h: payable.get(h, 0) - prior.get(h, 0)
            for h in set(payable) | set(prior)
        }

        latest = _latest_batch(conn, start, end)
        if not any(v != 0 for v in holder_deltas.values()):
            # 应付无变化（可能只是待处理清单变化）：不出新单、不生成归因证据
            return _build_response(
                latest, start, end, payable, prior, pending,
                note="no payable change since previous statement; no new batch created",
            )

        if _has_untraceable_batch(conn, start, end):
            # 期间存在无逐笔证据的存量单据：旧账无法逐笔归因，
            # 本追补单以"当前播放全量 + 旧账基线单列"冻结，不以当前播放冒充历史
            entries = _build_entries_for_detail(detail, "added")
            entries.extend(_build_legacy_baseline_entries(prior))
            attribution_mode = "legacy_anchored"
        else:
            # 正常 delta 模式：相对上次可追溯结果逐笔追加/撤销
            prior_by_tail, root_by_tail = _load_evidenced_payable(conn, start, end)
            entries = _build_delta_entries(detail, prior_by_tail, root_by_tail)
            attribution_mode = "delta"

        lines = {h: v for h, v in holder_deltas.items() if v != 0}
        batch_id, created_at = _insert_batch(
            conn, start, end, "supplement", latest["id"], lines,
            traceable=True, attribution_mode=attribution_mode,
        )
        _insert_entries(conn, batch_id, entries)
        batch = {
            "id": batch_id,
            "kind": "supplement",
            "supersedes_batch_id": latest["id"],
            "created_at": created_at,
            "traceable": True,
            "attribution_mode": attribution_mode,
        }
        return _build_response(batch, start, end, payable, prior, pending)


def preview_period(conn, start_in: datetime, end_in: datetime):
    """不出具，仅按当前数据试算。"""
    start, end, _ = canonical_period(start_in, end_in)
    payable, pending = compute_current(conn, start, end)
    prior = _load_prior_payable(conn, start, end)
    latest = _latest_batch(conn, start, end)
    return {
        "period_start": start,
        "period_end": end,
        "payable": [
            {
                "holder_code": h,
                "amount": payable.get(h, 0),
                "delta": (payable.get(h, 0) - prior.get(h, 0))
                if latest is not None else None,
            }
            for h in sorted(set(payable) | set(prior))
        ],
        "pending": pending,
        "statement_exists": latest is not None,
    }


def list_batches(conn, start_in: datetime, end_in: datetime):
    start, end, _ = canonical_period(start_in, end_in)
    batches = conn.execute(
        """
        SELECT id, kind, supersedes_batch_id, created_at, traceable, attribution_mode
        FROM statement_batches
        WHERE period_start = %s AND period_end = %s
        ORDER BY id
        """,
        (start, end),
    ).fetchall()
    result = []
    last_idx = len(batches) - 1
    for idx, b in enumerate(batches):
        lines = conn.execute(
            "SELECT holder_code, amount FROM statement_lines WHERE batch_id = %s ORDER BY holder_code",
            (b["id"],),
        ).fetchall()
        pending = _load_pending_snapshot(conn, start, end) if idx == last_idx else []
        result.append(
            {
                "batch_id": b["id"],
                "kind": b["kind"],
                "supersedes_batch_id": b["supersedes_batch_id"],
                "created_at": b["created_at"],
                "traceable": b["traceable"],
                "attribution_mode": b["attribution_mode"],
                "lines": [
                    {"holder_code": l["holder_code"], "amount": l["amount"], "delta": None}
                    for l in lines
                ],
                "pending": pending,
            }
        )
    return result


def _validate_page(page: int, page_size: int) -> tuple[int, int]:
    if not isinstance(page, int) or page < 1:
        raise APIError(422, "invalid_pagination", "page must be a positive integer (>=1)")
    if not isinstance(page_size, int) or page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise APIError(
            422, "invalid_pagination",
            f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}",
        )
    return page, page_size


def _load_batch_meta(conn, batch_id: int) -> dict:
    batch = conn.execute(
        """
        SELECT id, kind, supersedes_batch_id, period_start, period_end,
               created_at, traceable, attribution_mode
        FROM statement_batches WHERE id = %s
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise APIError(404, "statement_not_found", f"statement batch {batch_id} not found")
    return batch


def list_attribution(conn, batch_id: int, holder: str | None, page: int, page_size: int):
    """按单据（可限定权利人）分页查询归因记录与合计。

    以"更正链原始编号"为归集组；旧账无法归因基线单列一个虚拟组。
    汇总金额与该单据 statement_lines 明细行严格一致（逐笔守恒）。
    证据为出具时冻结的快照，历史记录不随日后数据变化。
    """
    page, page_size = _validate_page(page, page_size)
    batch = _load_batch_meta(conn, batch_id)

    # 原始明细行（权威合计来源）
    line_rows = conn.execute(
        "SELECT holder_code, amount FROM statement_lines WHERE batch_id = %s",
        (batch_id,),
    ).fetchall()
    lines_by_holder = {r["holder_code"]: r["amount"] for r in line_rows}

    if not batch["traceable"]:
        # 存量无逐笔证据的单据：明确标明不可追溯，仅返回原有明细行
        holders = sorted(lines_by_holder)
        if holder is not None:
            holders = [h for h in holders if h == holder]
        totals = [
            {"holder_code": h, "amount": lines_by_holder[h], "entry_count": 0}
            for h in holders
        ]
        return {
            "batch_id": batch["id"],
            "kind": batch["kind"],
            "supersedes_batch_id": batch["supersedes_batch_id"],
            "period_start": batch["period_start"],
            "period_end": batch["period_end"],
            "issued_at": batch["created_at"],
            "traceable": False,
            "attribution_mode": batch["attribution_mode"],
            "page": page,
            "page_size": page_size,
            "total_groups": 0,
            "total_entries": 0,
            "items": [],
            "holder_totals": totals,
            "note": "legacy statement without per-play evidence; not traceable",
        }

    # 取本单据全部证据（单单据证据量可控），内存中分组、过滤、分页。
    # 组键含 tail_import_id：同一原始链在更正追补单中会同时出现
    # "旧链尾撤销"与"新链尾新增"两个可辨组。
    entry_rows = conn.execute(
        """
        SELECT holder_code, chain_root_id, tail_import_id, work_code, occurred_at,
               agreement_id, entry_type, change_type, amount
        FROM statement_attribution_entries
        WHERE batch_id = %s
        ORDER BY chain_root_id NULLS LAST, tail_import_id NULLS LAST, holder_code, id
        """,
        (batch_id,),
    ).fetchall()

    # 组键：('play', root_id, tail_id) 或 ('legacy', None, None)
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for r in entry_rows:
        if holder is not None and r["holder_code"] != holder:
            continue
        if r["entry_type"] == "legacy_baseline":
            key = ("legacy", None, None)
        else:
            key = ("play", r["chain_root_id"], r["tail_import_id"])
        if key not in groups:
            if key[0] == "legacy":
                groups[key] = {
                    "entry_type": "legacy_baseline",
                    "chain_root_id": None,
                    "tail_import_id": None,
                    "work_code": None,
                    "occurred_at": None,
                    "agreement_id": None,
                    "entries": [],
                }
            else:
                groups[key] = {
                    "entry_type": "play",
                    "chain_root_id": r["chain_root_id"],
                    "tail_import_id": r["tail_import_id"],
                    "work_code": r["work_code"],
                    "occurred_at": r["occurred_at"],
                    "agreement_id": r["agreement_id"],
                    "entries": [],
                }
            order.append(key)
        groups[key]["entries"].append(
            {
                "holder_code": r["holder_code"],
                "amount": r["amount"],
                "change_type": r["change_type"],
            }
        )

    # 播放组按原始编号、链尾编号排序（撤销组无发生时间，紧随同链新增组）；
    # 旧账基线组始终置末
    def _group_sort_key(k):
        g = groups[k]
        if k[0] == "legacy":
            return (1, "", "")
        return (0, g["chain_root_id"] or "", g["tail_import_id"] or "")

    ordered_keys = sorted(order, key=_group_sort_key)

    total_groups = len(ordered_keys)
    start_idx = (page - 1) * page_size
    page_keys = ordered_keys[start_idx:start_idx + page_size]

    items = []
    for k in page_keys:
        g = groups[k]
        group_total = sum(e["amount"] for e in g["entries"])
        items.append(
            {
                "entry_type": g["entry_type"],
                "chain_root_id": g["chain_root_id"],
                "tail_import_id": g["tail_import_id"],
                "work_code": g["work_code"],
                "occurred_at": g["occurred_at"],
                "agreement_id": g["agreement_id"],
                "group_amount": group_total,
                "entries": g["entries"],
            }
        )

    # 合计：按权利人汇总证据。限定权利人时只回该权利人；
    # 无过滤时须与该单据原有明细行逐行一致。
    ev_by_holder: dict[str, int] = {}
    entry_count = 0
    for r in entry_rows:
        if holder is not None and r["holder_code"] != holder:
            continue
        ev_by_holder[r["holder_code"]] = ev_by_holder.get(r["holder_code"], 0) + r["amount"]
        entry_count += 1

    if holder is not None:
        holder_totals = (
            [{"holder_code": holder, "amount": ev_by_holder[holder],
              "entry_count": sum(1 for r in entry_rows if r["holder_code"] == holder)}]
            if holder in ev_by_holder else []
        )
    else:
        # 一致性自检：证据汇总必须与明细行完全相同，否则视为服务端数据损坏
        if ev_by_holder != lines_by_holder:
            raise APIError(
                500, "attribution_inconsistent",
                "attribution evidence totals do not match statement lines",
                detail={
                    "evidence_totals": ev_by_holder,
                    "line_totals": lines_by_holder,
                },
            )
        holder_totals = [
            {"holder_code": h, "amount": lines_by_holder[h],
             "entry_count": sum(1 for r in entry_rows if r["holder_code"] == h)}
            for h in sorted(lines_by_holder)
        ]

    return {
        "batch_id": batch["id"],
        "kind": batch["kind"],
        "supersedes_batch_id": batch["supersedes_batch_id"],
        "period_start": batch["period_start"],
        "period_end": batch["period_end"],
        "issued_at": batch["created_at"],
        "traceable": True,
        "attribution_mode": batch["attribution_mode"],
        "page": page,
        "page_size": page_size,
        "total_groups": total_groups,
        "total_entries": entry_count,
        "items": items,
        "holder_totals": holder_totals,
    }
