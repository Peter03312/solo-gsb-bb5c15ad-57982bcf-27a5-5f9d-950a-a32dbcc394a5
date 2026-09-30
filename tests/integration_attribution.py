"""逐笔归因证据端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_attribution.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_attr_pg_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    from fastapi.testclient import TestClient

    from app.main import app
    from app.db import pool

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    PERIOD = {"start": "2026-02-01T00:00:00+08:00",
              "end": "2026-03-01T00:00:00+08:00"}

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 搭数据：作品 W，两条播放 P1(100) P2(80)，先无协议 ----
        c.post("/works", json={"code": "W", "title": "归因测试作品"})
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        c.post("/plays/import", json={
            "import_id": "P2", "work_code": "W",
            "occurred_at": "2026-02-11T20:00:00+08:00", "amount": 80})

        # 初次出具：无协议 => 待处理、无应付（待处理不计入应付）
        r = c.post("/statements/issue", json=PERIOD)
        b1 = r.json()
        check("initial pending only",
              b1["kind"] == "initial" and b1["payable"] == []
              and {p["import_id"] for p in b1["pending"]} == {"P1", "P2"},
              r.text)
        check("initial traceable flag", b1["traceable"] is True)

        # 登记协议 62.5/30/7.5
        r = c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "62.5"},
                       {"holder_code": "B", "percent": "30"},
                       {"holder_code": "C", "percent": "7.5"}]})
        check("agreement", r.status_code == 201, r.text)
        agreement_id = r.json()["id"]

        # 追补 1：协议后补，两条播放从待处理转应付
        # 100 => 63/30/7；80 => 50/24/6；合计 A=113 B=54 C=13
        r = c.post("/statements/issue", json=PERIOD)
        b2 = r.json()
        check("supp1 kind", b2["kind"] == "supplement" and b2["attribution_mode"] == "delta",
              r.text)

        # ---- 归因查询：按原始编号归集，两组 ----
        r = c.get(f"/statements/{b2['batch_id']}/attribution")
        check("attr 200", r.status_code == 200, r.text)
        attr = r.json()
        check("attr 2 groups", attr["total_groups"] == 2, str(attr["total_groups"]))
        check("attr 6 entries", attr["total_entries"] == 6, str(attr["total_entries"]))
        roots = {g["chain_root_id"]: g for g in attr["items"]}
        check("roots are original ids", set(roots) == {"P1", "P2"}, str(set(roots)))
        g1 = roots["P1"]
        check("frozen tail/work/time/agreement",
              g1["tail_import_id"] == "P1" and g1["work_code"] == "W"
              and g1["agreement_id"] == agreement_id
              and g1["occurred_at"].startswith("2026-02-10"),
              str(g1))
        alloc1 = {e["holder_code"]: e["amount"] for e in g1["entries"]}
        check("P1 alloc 63/30/7", alloc1 == {"A": 63, "B": 30, "C": 7}, str(alloc1))
        check("all added", all(e["change_type"] == "added" for e in g1["entries"]))
        alloc2 = {e["holder_code"]: e["amount"]
                  for e in roots["P2"]["entries"]}
        check("P2 alloc 50/24/6", alloc2 == {"A": 50, "B": 24, "C": 6}, str(alloc2))

        # 合计须与单据原有明细行一致
        totals = {t["holder_code"]: t["amount"] for t in attr["holder_totals"]}
        lines = {l["holder_code"]: l["amount"] for l in b2["payable"]}
        check("totals equal payable current view is not the check; "
              "use lines", totals == {"A": 113, "B": 54, "C": 13}, str(totals))
        # 明细行存的是差额(=当前应付)
        check("evidence sums == statement lines", totals == lines, f"{totals} vs {lines}")

        # 单据列表也带 traceable/attribution_mode
        r = c.get("/statements", params=PERIOD)
        lst = r.json()
        check("list flags",
              all("traceable" in b and "attribution_mode" in b for b in lst))

        # ---- 按权利人过滤 ----
        r = c.get(f"/statements/{b2['batch_id']}/attribution?holder_code=A")
        a_attr = r.json()
        check("holder filter totals",
              len(a_attr["holder_totals"]) == 1
              and a_attr["holder_totals"][0]["holder_code"] == "A"
              and a_attr["holder_totals"][0]["amount"] == 113,
              str(a_attr["holder_totals"]))
        check("holder filter entries only A",
              all(e["holder_code"] == "A"
                  for g in a_attr["items"] for e in g["entries"]))
        check("holder filter 2 groups", a_attr["total_groups"] == 2)

        # 不存在的权利人：空结果但合计为空（非报错）
        r = c.get(f"/statements/{b2['batch_id']}/attribution?holder_code=NOBODY")
        check("unknown holder empty",
              r.status_code == 200 and r.json()["items"] == []
              and r.json()["holder_totals"] == [])

        # ---- 分页 ----
        r = c.get(f"/statements/{b2['batch_id']}/attribution?page=1&page_size=1")
        pg = r.json()
        check("page1 size1", len(pg["items"]) == 1 and pg["total_groups"] == 2
              and pg["page"] == 1 and pg["page_size"] == 1)
        r = c.get(f"/statements/{b2['batch_id']}/attribution?page=2&page_size=1")
        check("page2 size1", len(r.json()["items"]) == 1)
        r = c.get(f"/statements/{b2['batch_id']}/attribution?page=99&page_size=1")
        check("page beyond empty", r.json()["items"] == [])

        # 非法分页参数 => 422 明确报错
        r = c.get(f"/statements/{b2['batch_id']}/attribution?page=0")
        check("page 0 -> 422", r.status_code == 422
              and r.json()["error"] == "invalid_pagination", r.text)
        r = c.get(f"/statements/{b2['batch_id']}/attribution?page_size=0")
        check("page_size 0 -> 422", r.status_code == 422, r.text)
        r = c.get(f"/statements/{b2['batch_id']}/attribution?page_size=9999")
        check("page_size too big -> 422", r.status_code == 422, r.text)
        r = c.get(f"/statements/{b2['batch_id']}/attribution?page=abc")
        check("page nan -> 422", r.status_code == 422, r.text)

        # 不存在的单据 => 404
        r = c.get("/statements/999999/attribution")
        check("missing batch 404",
              r.status_code == 404 and r.json()["error"] == "statement_not_found",
              r.text)
        # 非法单据 id => 422（类型校验）
        r = c.get("/statements/not-an-int/attribution")
        check("bad batch id 422", r.status_code == 422, r.text)

        # ---- 跨期更正：P1 -> P1R1 金额 30（19/9/2），P2 不变 ----
        r = c.post("/plays/import", json={
            "import_id": "P1R1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 30,
            "corrects_id": "P1"})
        check("correction", r.status_code == 201, r.text)
        r = c.post("/statements/issue", json=PERIOD)
        b3 = r.json()
        # 当前应付：30 => 19/9/2；80 => 50/24/6 => A=69 B=33 C=8
        got = {x["holder_code"]: (x["amount"], x["delta"]) for x in b3["payable"]}
        check("supp2 deltas",
              got == {"A": (69, -44), "B": (33, -21), "C": (8, -5)}, str(got))

        # 归因：同一原始链 P1 下出现两个可辨组：旧 tail 撤销(负)、新 tail 新增(正)
        r = c.get(f"/statements/{b3['batch_id']}/attribution")
        attr3 = r.json()
        check("supp2 revoke+add groups for same root",
              attr3["total_groups"] == 2,
              str([(g["chain_root_id"], g["tail_import_id"]) for g in attr3["items"]]))
        g_p1_new = next(g for g in attr3["items"]
                        if g["chain_root_id"] == "P1" and g["tail_import_id"] == "P1R1")
        g_p1_old = next(g for g in attr3["items"]
                        if g["chain_root_id"] == "P1" and g["tail_import_id"] == "P1")
        check("new-tail group carries frozen play facts",
              g_p1_new["work_code"] == "W" and g_p1_new["agreement_id"] == agreement_id
              and g_p1_new["occurred_at"].startswith("2026-02-10"),
              str(g_p1_new))
        new_changes = {(e["holder_code"], e["change_type"]): e["amount"]
                       for e in g_p1_new["entries"]}
        check("new tail added 19/9/2",
              new_changes == {("A", "added"): 19, ("B", "added"): 9, ("C", "added"): 2},
              str(new_changes))
        # 旧链尾撤销：不冒充播放（作品/时间/协议为空），负额保留
        check("old-tail revoked facts cleared",
              g_p1_old["work_code"] is None and g_p1_old["occurred_at"] is None
              and g_p1_old["agreement_id"] is None,
              str(g_p1_old))
        old_changes = {(e["holder_code"], e["change_type"]): e["amount"]
                       for e in g_p1_old["entries"]}
        check("revoke negatives kept",
              old_changes == {("A", "revoked"): -63, ("B", "revoked"): -30,
                              ("C", "revoked"): -7},
              str(old_changes))
        check("old group amount = -100", g_p1_old["group_amount"] == -100,
              str(g_p1_old["group_amount"]))
        check("new group amount = 30", g_p1_new["group_amount"] == 30,
              str(g_p1_new["group_amount"]))
        # P2 无变化 => 不出现在追补证据中
        check("unchanged P2 absent",
              all(g["chain_root_id"] != "P2" for g in attr3["items"]))
        # 合计仍须等于明细行 -44/-21/-5
        t3 = {t["holder_code"]: t["amount"] for t in attr3["holder_totals"]}
        check("supp2 totals equal lines",
              t3 == {"A": -44, "B": -21, "C": -5}, str(t3))

        # ---- 历史不可变：再更正并出具后，旧单据归因快照不变 ----
        c.post("/plays/import", json={
            "import_id": "P1R2", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 10,
            "corrects_id": "P1R1"})
        c.post("/statements/issue", json=PERIOD)
        r = c.get(f"/statements/{b3['batch_id']}/attribution")
        frozen = r.json()
        # b3 出具时冻结的"新链尾"仍是 P1R1（added 组），日后再更正不改变它
        added_tails = {
            g["tail_import_id"]
            for g in frozen["items"]
            if any(e["change_type"] == "added" for e in g["entries"])
        }
        check("history immutable after later correction", added_tails == {"P1R1"},
              str(added_tails))

        # ---- 无应付变化不生成新单据也不生成证据 ----
        before = c.get("/statements", params=PERIOD).json()
        r = c.post("/statements/issue", json=PERIOD)
        check("no change returns latest",
              r.json()["batch_id"] == before[-1]["batch_id"])
        after = c.get("/statements", params=PERIOD).json()
        check("no new batch", len(after) == len(before))

        # ---- 纯待处理期间：不计入应付，不出任何归因证据 ----
        c.post("/works", json={"code": "WP", "title": "无协议作品"})
        c.post("/plays/import", json={
            "import_id": "Q1", "work_code": "WP",
            "occurred_at": "2026-06-12T20:00:00+08:00", "amount": 7})
        pp = {"start": "2026-06-01T00:00:00+08:00",
              "end": "2026-06-20T00:00:00+08:00"}
        bp = c.post("/statements/issue", json=pp).json()
        ap = c.get(f"/statements/{bp['batch_id']}/attribution").json()
        check("pending plays yield no evidence",
              bp["payable"] == [] and len(bp["pending"]) == 1
              and ap["total_entries"] == 0 and ap["holder_totals"] == []
              and ap["traceable"] is True, str(ap))

        # ---- 更正在零金额：当前应付归零，旧链尾负额撤销必须保留 ----
        c.post("/works", json={"code": "WZ", "title": "零额更正"})
        c.post("/agreements", json={
            "work_code": "WZ",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "50"},
                       {"holder_code": "B", "percent": "50"}]})
        pz = {"start": "2026-04-01T00:00:00+08:00",
              "end": "2026-05-01T00:00:00+08:00"}
        c.post("/plays/import", json={
            "import_id": "Z1", "work_code": "WZ",
            "occurred_at": "2026-04-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=pz)
        c.post("/plays/import", json={
            "import_id": "Z1R1", "work_code": "WZ",
            "occurred_at": "2026-04-10T20:00:00+08:00", "amount": 0,
            "corrects_id": "Z1"})
        bz = c.post("/statements/issue", json=pz).json()
        check("zero-amount supplement delta",
              {x["holder_code"]: x["delta"] for x in bz["payable"]}
              == {"A": -50, "B": -50}, str(bz["payable"]))
        az = c.get(f"/statements/{bz['batch_id']}/attribution").json()
        zalloc = {
            (g["tail_import_id"], e["holder_code"]): (e["amount"], e["change_type"])
            for g in az["items"] for e in g["entries"]
        }
        check("zero correction keeps negative revocation",
              zalloc == {("Z1", "A"): (-50, "revoked"),
                         ("Z1", "B"): (-50, "revoked")},
              str(zalloc))

        # ---- 存量旧单：使用独立旧期间，直接在库里造一张功能上线前的旧单据 ----
        # （无 statement_attribution_entries，traceable=false）
        LEGACY_PERIOD = ("2025-05-01T00:00:00+08:00", "2025-06-01T00:00:00+08:00")

        def _utc(s):
            from datetime import datetime, timezone
            return datetime.fromisoformat(s).astimezone(timezone.utc)

        from app.db import pool as svc_pool

        with svc_pool.connection() as conn:
            with conn.transaction():
                r = conn.execute(
                    """
                    INSERT INTO statement_batches(period_start, period_end, kind, traceable)
                    VALUES (%s, %s, 'initial', false) RETURNING id
                    """,
                    (_utc(LEGACY_PERIOD[0]), _utc(LEGACY_PERIOD[1])),
                ).fetchone()
                legacy_id = r["id"]
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO statement_lines(batch_id, holder_code, amount)"
                        " VALUES (%s, %s, %s)",
                        [(legacy_id, "A", 100), (legacy_id, "B", 40)],
                    )

        # 旧单归因查询：明确不可追溯
        r = c.get(f"/statements/{legacy_id}/attribution")
        leg = r.json()
        check("legacy marked untraceable",
              r.status_code == 200 and leg["traceable"] is False
              and "not traceable" in (leg["note"] or ""), r.text)
        check("legacy still shows original lines",
              {t["holder_code"]: t["amount"] for t in leg["holder_totals"]}
              == {"A": 100, "B": 40}, str(leg["holder_totals"]))

        # 旧期间准备当前播放与协议（旧账期间本身无播放记录，模拟"现在能算"）
        c.post("/works", json={"code": "WL", "title": "旧账作品"})
        c.post("/agreements", json={
            "work_code": "WL",
            "valid_from": "2025-01-01T00:00:00+08:00",
            "valid_to": "2026-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "70"},
                       {"holder_code": "B", "percent": "30"}]})
        c.post("/plays/import", json={
            "import_id": "LP1", "work_code": "WL",
            "occurred_at": "2025-05-10T20:00:00+08:00", "amount": 200})
        # 当前应付 A=140 B=60；旧单累计 A=100 B=40 => 差额 A=40 B=20
        period_legacy = {"start": LEGACY_PERIOD[0], "end": LEGACY_PERIOD[1]}
        r = c.post("/statements/issue", json=period_legacy)
        bs = r.json()
        check("legacy-anchored supplement",
              bs["kind"] == "supplement"
              and bs["attribution_mode"] == "legacy_anchored", r.text)
        got = {x["holder_code"]: x["delta"] for x in bs["payable"]}
        check("legacy supplement deltas 40/20", got == {"A": 40, "B": 20}, str(got))

        r = c.get(f"/statements/{bs['batch_id']}/attribution")
        attr_s = r.json()
        types = {g["entry_type"] for g in attr_s["items"]}
        check("both play and legacy groups",
              types == {"play", "legacy_baseline"}, str(types))
        play_g = next(g for g in attr_s["items"] if g["entry_type"] == "play")
        base_g = next(g for g in attr_s["items"] if g["entry_type"] == "legacy_baseline")
        check("current play fully attributed",
              play_g["chain_root_id"] == "LP1"
              and {e["holder_code"]: e["amount"] for e in play_g["entries"]}
              == {"A": 140, "B": 60}, str(play_g))
        check("legacy baseline separate, negated, no fake play",
              base_g["chain_root_id"] is None and base_g["tail_import_id"] is None
              and {e["holder_code"]: e["amount"] for e in base_g["entries"]}
              == {"A": -100, "B": -40},
              str(base_g))
        # 证据合计 = 明细行差额 40/20
        tt = {t["holder_code"]: t["amount"] for t in attr_s["holder_totals"]}
        check("legacy-anchored totals equal lines",
              tt == {"A": 40, "B": 20}, str(tt))

        # 其后再出具（有新更正）=> 回归普通 delta，基线不重复
        c.post("/plays/import", json={
            "import_id": "LP1R1", "work_code": "WL",
            "occurred_at": "2025-05-10T20:00:00+08:00", "amount": 100,
            "corrects_id": "LP1"})
        # 当前 A=70 B=30；累计旧100/40 + 追补40/20 = 140/60 => 差额 -70/-30
        r = c.post("/statements/issue", json=period_legacy)
        b4 = r.json()
        check("second supplement back to delta",
              b4["attribution_mode"] == "delta", r.text)
        r = c.get(f"/statements/{b4['batch_id']}/attribution")
        a4 = r.json()
        check("no second legacy baseline",
              all(g["entry_type"] == "play" for g in a4["items"]),
              str([g["entry_type"] for g in a4["items"]]))
        t4 = {t["holder_code"]: t["amount"] for t in a4["holder_totals"]}
        check("delta2 totals -70/-30", t4 == {"A": -70, "B": -30}, str(t4))

        # ---- 并发出具不重复生成证据 ----
        c.post("/works", json={"code": "WC", "title": "并发归因"})
        c.post("/agreements", json={
            "work_code": "WC",
            "valid_from": "2028-01-01T00:00:00+08:00",
            "valid_to": "2029-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        c.post("/plays/import", json={
            "import_id": "CP1", "work_code": "WC",
            "occurred_at": "2028-02-01T20:00:00+08:00", "amount": 55})
        pconcurrent = {"start": "2028-02-01T00:00:00+08:00",
                       "end": "2028-03-01T00:00:00+08:00"}
        ids = []

        def fire():
            rr = c.post("/statements/issue", json=pconcurrent)
            ids.append(rr.json().get("batch_id"))

        ts = [threading.Thread(target=fire) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("concurrent one batch", len(set(ids)) == 1, str(ids))
        bid = ids[0]
        with svc_pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) AS n FROM statement_attribution_entries WHERE batch_id = %s",
                (bid,),
            ).fetchone()["n"]
            bn = conn.execute(
                "SELECT count(*) AS n FROM statement_batches "
                "WHERE period_start=%s AND period_end=%s",
                (_utc(pconcurrent["start"]), _utc(pconcurrent["end"])),
            ).fetchone()["n"]
        check("no duplicate evidence rows", n == 1, f"entries={n}")
        check("only one batch row", bn == 1, f"batches={bn}")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL ATTRIBUTION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
