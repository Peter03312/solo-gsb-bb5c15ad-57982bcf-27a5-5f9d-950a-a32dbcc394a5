# 演出录音播放版税结算服务

Python + FastAPI + PostgreSQL 实现的播放版税结算服务，支持：

- 作品与权利人登记，分成协议登记（适用起止时间、多名权利人及各自比例）；
  - 同一作品协议区间不得重叠（"每作品事务咨询锁 + 区间重叠检测"强制，**无需任何 contrib 扩展**）；
  - 每笔协议比例须合计 **100%**（应用层 + 数据库延迟约束触发器双重校验）。
- 播放记录导入：调用方唯一编号、作品、发生时间、非负整数金额（最小货币单位）。
  - 同编号同内容 **幂等**；同编号异内容 **拒绝（409）**；
  - 更正必须引用原编号（`corrects_id`）并使用新编号；**分叉、循环更正拒绝**；
    原记录留痕，仅更正链 **链尾** 参与核算。
- 按播放 **发生时间** 匹配协议；缺协议的播放列入 **待处理清单**，不计入应付。
- 逐笔按比例分摊到最小货币单位：先向下取整，余数按 **小数部分降序、权利人编号升序** 补足，逐笔总额严格守恒。
- 按期间出具权利人应付额与待处理清单：
  - 已出具单据 **不可覆盖**；
  - 后补协议或播放更正后，再出具按 **新应付额 − 此前单据累计额** 出具 **追补单**，差额 **可正可负**；
  - 应付无变化不产生新单据；同一期间并发出具经咨询锁串行化，**不会产生重复单据**。
- **逐笔归因证据（本轮新增）**：每次出具时按更正链 **原始编号** 归集并 **冻结** 参与核算的
  链尾编号、作品、发生时间、匹配协议与逐笔分摊，供财务核对"每位权利人的钱来自哪些播放"：
  - 追补单逐笔标注 **新增（added）/ 撤销（revoked）**，跨期更正形成"旧链尾撤销（负）+
    新链尾新增（正）"两个可辨组，协议后补使原待处理播放转为新增；**负额照存不丢失**；
  - 归因合计与该单据 **原有明细行严格一致**；历史证据为快照，**不随日后数据变化**；
    待处理播放不计入应付，也不产生归因；
  - 应付无变化不生成新单据，也 **不生成新证据**；并发出具不会重复生成证据；
  - 功能上线前 **无逐笔证据的存量单据标记为不可追溯（traceable=false）**；其后首张追补单
    以 `legacy_anchored` 模式把当前播放全量归因、并把 **无法归因的旧账差额单列**
    （`legacy_baseline`，旧账取负），**不以当前播放冒充历史**；锚定一次后回归普通逐笔差额。

## 一键启动

要求 Docker 20.10+（带 Compose v2）。

```bash
docker compose up --build -d
```

首次启动会自动：

1. 启动 PostgreSQL 16 并建立数据卷；
2. 等待数据库健康检查通过；
3. 构建并启动 API 容器，自动执行建表脚本（约束、触发器，无需额外扩展）。

查看日志：

```bash
docker compose logs -f api
```

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据库卷
```

## 地址与端口

| 服务 | 默认地址 |
| --- | --- |
| API 基地址 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger UI（交互式文档） | http://localhost:8000/docs |
| OpenAPI JSON | http://localhost:8000/openapi.json |
| PostgreSQL | localhost:5432 |

## 配置（可选）

均通过环境变量覆盖，docker-compose 已内置默认值；可在项目根目录放 `.env`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8000` | API 映射到宿主机的端口 |
| `POSTGRES_PORT` | `5432` | 数据库映射到宿主机的端口 |
| `POSTGRES_USER` | `royalty` | 数据库用户 |
| `POSTGRES_PASSWORD` | `royalty` | 数据库密码 |
| `POSTGRES_DB` | `royalty` | 数据库名 |
| `DATABASE_URL`（仅 API 容器） | `postgresql://royalty:royalty@db:5432/royalty` | 数据库连接串 |

本地裸机运行（不使用 Docker）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 让 DATABASE_URL 指向本地 PG（无需安装任何 contrib 扩展）
export DATABASE_URL="postgresql://royalty:royalty@localhost:5432/royalty"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 金额一律使用 **非负整数、最小货币单位**（如"分"），避免浮点误差；比例以字符串小数传输（如 `"62.5"`），最多 4 位小数（0.0001%）。
> 所有时间必须带时区（如 `2026-02-10T20:00:00+08:00`），内部统一按 UTC 比较；协议区间为半开 `[from, to)`，相邻区间允许，未填 `valid_to` 表示长期有效。

## HTTP API 一览

### 登记

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/works` | 登记作品（同编号幂等，标题冲突 409） |
| GET | `/works` | 作品列表 |
| POST | `/rightsholders` | 登记权利人（建协议时也会自动建档） |
| GET | `/rightsholders` | 权利人列表 |
| POST | `/agreements` | 登记分成协议（区间重叠 409；比例合计≠100% 422） |
| GET | `/agreements?work_code=` | 协议列表（可按作品过滤） |
| GET | `/agreements/{id}` | 协议详情 |

### 播放记录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plays/import` | 导入/更正单条；幂等返回 `status=duplicate`，异内容 409，分叉 409 |
| POST | `/plays/bulk-import` | 批量导入（整批一个事务，任一失败整批拒绝） |
| GET | `/plays/{import_id}/chain` | 更正链（从原始记录到链尾，标注 `chain_tail`） |

### 结算单据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/statements/issue` | 按期间出具；首次初次单，其后差额追补单（无变化不出新单），同时冻结逐笔归因证据 |
| GET | `/statements/preview?start=&end=` | 不出具，试算当前应付、待处理、潜在差额 |
| GET | `/statements?start=&end=` | 列出该期间全部单据及明细（含 traceable/attribution_mode 标记） |
| GET | `/statements/{batch_id}/attribution` | **按单据分页查询逐笔归因记录与权利人合计** |

期间同样是半开区间 `[start, end)`，与协议区间语义一致。

#### 逐笔归因查询 `GET /statements/{batch_id}/attribution`

查询参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `holder_code` | 无 | 只看某一权利人（可选；不存在的权利人返回空结果，不报错） |
| `page` | `1` | 页码，从 1 开始；`<1` 返回 422 |
| `page_size` | `50` | 每页归集组数，范围 `1–200`；越界返回 422 |

返回要点：

- `items[]`：按 **更正链原始编号 `chain_root_id` + 出具时链尾 `tail_import_id`** 归集的组。
  同一链在追补单中若发生更正，会出现"旧链尾撤销组（金额为负）"与"新链尾新增组"两个可辨组；
  每组含冻结的作品 `work_code`、发生时间 `occurred_at`、匹配协议 `agreement_id`、
  组合计 `group_amount` 及逐权利人 `entries[]`（`amount` + `change_type`）。
- `holder_totals[]`：按权利人合计的金额与证据条数；**无 `holder_code` 过滤时与该单据
  `statement_lines` 明细行逐行一致**（服务端会做一致性自检，不一致报 500）。
- `total_groups` / `total_entries` / `page` / `page_size`：分页信息。
- 存量旧单返回 `traceable=false`、`items=[]`、`note` 注明 **not traceable**，
  `holder_totals` 仍给出原有明细行金额。
- 不存在的单据 `404 statement_not_found`；`batch_id` 非整数 `422`。

## 调用示例

一键演示脚本（请先启动服务），覆盖幂等、异内容拒绝、区间重叠、更正链/分叉拒绝、
后补协议、逐笔分摊、初次单与正负追补单、待处理清单等全部场景：

```bash
bash examples/demo.sh
```

手工调用（以下为关键几步）：

```bash
# 1. 登记作品
curl -sS -X POST http://localhost:8000/works \
  -H 'Content-Type: application/json' \
  -d '{"code":"W-LIVE-001","title":"仲夏之夜现场录音"}'

# 2. 登记分成协议（合计 100%）
curl -sS -X POST http://localhost:8000/agreements \
  -H 'Content-Type: application/json' \
  -d '{
    "work_code":"W-LIVE-001",
    "valid_from":"2026-01-01T00:00:00+08:00",
    "valid_to":"2027-01-01T00:00:00+08:00",
    "shares":[
      {"holder_code":"RS-A","percent":"62.5"},
      {"holder_code":"RS-B","percent":"30"},
      {"holder_code":"RS-C","percent":"7.5"}
    ]
  }'

# 3. 导入播放（100 分）
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'

# 4. 更正：新编号 + corrects_id 引用原编号
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R1","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":80,
       "corrects_id":"PLAY-1001"}'

# 5. 出具 2026 年 2 月单据（80 分 => 50/24/6）
curl -sS -X POST http://localhost:8000/statements/issue \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'

# 6. 再次更正为 30 分后重新出具 => 追补单差额 -31/-15/-4
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R2","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":30,
       "corrects_id":"PLAY-1001-R1"}'
curl -sS -X POST http://localhost:8000/statements/issue \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'

# 7. 查这张单据的逐笔归因（金额来自哪些播放；分页参数 page/page_size，可选 holder_code）
curl -sS "http://localhost:8000/statements/1/attribution?page=1&page_size=50"
```

## 分摊示例

金额 100，比例 62.5% / 30% / 7.5%：

- 精确值 62.5 / 30 / 7.5 → 先取整 62 / 30 / 7，余 1；
- 小数部分 `RS-A`、`RS-C` 同为 0.5（`RS-B` 为 0），按权利人编号升序补 1 给 `RS-A`；
- 结果 **63 / 30 / 7 = 100**。

金额 80：50 / 24 / 6（精确整除，无余数）；金额 30：18.75 / 9 / 2.25，
取整 18/9/2 余 1，小数部分最大的 `RS-A`（0.75）补 1 → **19/9/2**。每笔之和恒等于原金额。

## 测试

核心分摊算法的单元测试（无需数据库）：

```bash
python3 -m unittest discover -s tests -v
```

端到端集成测试（用 [`pgserver`](https://pypi.org/project/pgserver/) 自动拉起便携 PostgreSQL，
**无需 Docker / root**，覆盖登记、幂等、重叠、更正链、待处理、初次/追补单、并发出具等全链路）：

```bash
pip install pgserver httpx
PYTHONPATH=. python3 tests/integration_pgserver.py
```

逐笔归因证据的专项集成测试（按原始编号归集、跨期更正的撤销/新增与负额、合计与明细一致性、
分页与非法参数/不存在单据报错、历史快照不变、存量旧单不可追溯与旧账基线单列、
并发出具不重复证据）：

```bash
PYTHONPATH=. python3 tests/integration_attribution.py
```

## 目录结构

```
app/
  main.py                 # FastAPI 入口、生命周期、异常映射
  config.py               # 环境配置
  db.py                   # 连接池与幂等建表脚本（全部约束在此）
  allocation.py           # 逐笔按比例分摊（最大余数法 + 编号次序）
  schemas.py              # Pydantic 模型与输入校验
  statements_service.py   # 期间匹配、重算、初次/追补出具、逐笔归因冻结与分页查询、并发互斥
  routers/
    catalog.py            # 作品 / 权利人 / 协议（含协议区间互斥）
    plays.py              # 播放导入、幂等、更正链
    statements.py         # 单据出具、试算、查询、逐笔归因查询
examples/demo.sh          # 端到端 curl 示例（含归因查询）
tests/                    # 单元测试 + 两套 pgserver 端到端集成测试
Dockerfile  docker-compose.yml  requirements.txt
```
