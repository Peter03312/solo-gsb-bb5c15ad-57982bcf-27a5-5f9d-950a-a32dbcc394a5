#!/usr/bin/env bash
# 端到端调用示例。前置：docker compose up -d 已启动。
# 用法: BASE=http://localhost:8000 bash examples/demo.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 登记作品"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-LIVE-001","title":"仲夏之夜现场录音"}'; echo

say "2) 导入播放（此时还没有协议 => 之后会进待处理）"
# 100 分，权利人 A 62.5% / B 30% / C 7.5%（协议后补后：62/30，余 1 按小数部分给 C）
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo

say "3) 同编号同内容重复导入 => 幂等 duplicate"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo

say "4) 同编号异内容 => 409 拒绝"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":200}'; echo

say "5) 登记权利人（协议登记也会自动建档，此处显式登记可选）"
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RS-A","name":"甲唱片公司"}'; echo
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RS-B","name":"乙艺人"}'; echo
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RS-C","name":"丙词曲方"}'; echo

say "6) 登记分成协议（合计 100%，适用 2026 全年）"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[
    {"holder_code":"RS-A","percent":"62.5"},
    {"holder_code":"RS-B","percent":"30"},
    {"holder_code":"RS-C","percent":"7.5"}
  ]
}'; echo

say "7) 再登记一个与上者区间重叠的协议 => 409"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2026-03-01T00:00:00+08:00",
  "valid_to":"2026-05-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"100"}]
}'; echo

say "8) 比例合计不是 100% => 422"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"99"}]
}'; echo

say "9) 登记第二段相邻协议（半开区间，边界相接允许）"
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-LIVE-001",
  "valid_from":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"100"}]
}'; echo

say "10) 更正播放：引用原编号、给新编号（金额 100 -> 80，链尾变更）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R1","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":80,"corrects_id":"PLAY-1001"}'; echo

say "11) 再有人更正同一条原记录 => 分叉，409"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-RX","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":90,"corrects_id":"PLAY-1001"}'; echo

say "12) 查看更正链（原记录留痕，仅链尾参与核算）"
$C "$BASE/plays/PLAY-1001/chain" | python3 -m json.tool; echo

say "13) 试算 2026 年 2 月期间（80 分：50/24/6）"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" | python3 -m json.tool; echo

say "14) 出具初次单（50/24/6，无待处理，因为协议已后补）"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' | python3 -m json.tool; echo

say "15) 再次更正（80 -> 30），再出具 => 差额追补单（可正可负）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R2","work_code":"W-LIVE-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":30,"corrects_id":"PLAY-1001-R1"}'; echo
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' | python3 -m json.tool; echo

say "16) 数据无变化再出具 => 不产生新单据"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' | python3 -m json.tool; echo

say "17) 列出该期间全部单据（1 张初次 + 1 张追补）"
$C "$BASE/statements?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" | python3 -m json.tool; echo

say "18) 待处理清单：导入另一作品的播放（无协议）并对 3 月出具"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' -d '{"code":"W-LIVE-002","title":"安可集"}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-2001","work_code":"W-LIVE-002","occurred_at":"2026-03-05T21:00:00+08:00","amount":500}'; echo
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-03-01T00:00:00+08:00","end":"2026-04-01T00:00:00+08:00"}' | python3 -m json.tool; echo

say "完成"
