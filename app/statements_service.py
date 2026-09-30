import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from .allocation import allocate

# 咨询锁：使用单 bigint 键，高 32 位为服务命名空间、低 32 位为期间键
ADVISORY_NS = 0x524F5941  # "ROYA"


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


def compute_current(conn, start: datetime, end: datetime):
    """按当前最新数据（后补协议、更正后的链尾）重算期间应付与待处理。"""
    rows = conn.execute(
        EFFECTIVE_PLAYS_SQL, {"start": start, "end": end}
    ).fetchall()

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

    payable: dict[str, int] = {}
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
        parts = allocate(r["amount"], shares_map[r["agreement_id"]])
        for holder, amt in parts.items():
            payable[holder] = payable.get(holder, 0) + amt

    pending.sort(key=lambda x: (x["occurred_at"], x["import_id"]))
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
        SELECT id, kind, supersedes_batch_id, created_at
        FROM statement_batches
        WHERE period_start = %s AND period_end = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (start, end),
    ).fetchone()


def _insert_batch(conn, start, end, kind, supersedes, lines: dict[str, int]):
    row = conn.execute(
        """
        INSERT INTO statement_batches(period_start, period_end, kind, supersedes_batch_id)
        VALUES (%s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (start, end, kind, supersedes),
    ).fetchone()
    if lines:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO statement_lines(batch_id, holder_code, amount) VALUES (%s, %s, %s)",
                [(row["id"], holder, amt) for holder, amt in lines.items()],
            )
    return row["id"], row["created_at"]


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
        "payable": lines,
        "pending": pending,
    }


def issue_statement(conn, start_in: datetime, end_in: datetime):
    start, end, key = canonical_period(start_in, end_in)
    with conn.transaction():
        # 同一期间串行化出具，杜绝并发重复单据
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(ADVISORY_NS, _period_lock_key(key)),),
        )

        payable, pending = compute_current(conn, start, end)
        _insert_pending_snapshot(conn, start, end, pending)

        existing_initial = conn.execute(
            "SELECT id FROM statement_batches WHERE period_start = %s AND period_end = %s AND kind = 'initial'",
            (start, end),
        ).fetchone()

        if existing_initial is None:
            batch_id, created_at = _insert_batch(
                conn, start, end, "initial", None,
                {h: amt for h, amt in payable.items() if amt != 0},
            )
            batch = {
                "id": batch_id,
                "kind": "initial",
                "supersedes_batch_id": None,
                "created_at": created_at,
            }
            return _build_response(batch, start, end, payable, {}, pending)

        # 已有单据：不可覆盖，按新应付额与此前累计额之差出具追补单（差额可正可负）
        prior = _load_prior_payable(conn, start, end)
        holders = set(payable) | set(prior)
        deltas = {
            h: payable.get(h, 0) - prior.get(h, 0)
            for h in holders
            if payable.get(h, 0) - prior.get(h, 0) != 0
        }

        latest = _latest_batch(conn, start, end)
        if not deltas:
            # 应付无变化（可能只是待处理清单变化），不重复出单
            return _build_response(
                latest, start, end, payable, prior, pending,
                note="no payable change since previous statement; no new batch created",
            )

        batch_id, created_at = _insert_batch(
            conn, start, end, "supplement", latest["id"], deltas
        )
        batch = {
            "id": batch_id,
            "kind": "supplement",
            "supersedes_batch_id": latest["id"],
            "created_at": created_at,
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
        SELECT id, kind, supersedes_batch_id, created_at
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
                "lines": [
                    {"holder_code": l["holder_code"], "amount": l["amount"], "delta": None}
                    for l in lines
                ],
                "pending": pending,
            }
        )
    return result
