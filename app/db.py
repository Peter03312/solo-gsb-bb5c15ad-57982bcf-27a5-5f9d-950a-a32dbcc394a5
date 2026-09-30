import time
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

pool: ConnectionPool | None = None


# 幂等的建表脚本：服务首次启动时自动初始化全部结构
# 说明：排他约束中 text 列显式使用 GiST 的 gist_bbox_ops 操作符类（标量等值），
# 因此不需要 contrib 的 btree_gist 扩展。
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS works (
    code       text PRIMARY KEY,
    title      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rightsholders (
    code       text PRIMARY KEY,
    name       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 分成协议：半开区间 [valid_from, valid_to)，valid_to 为空表示长期有效
-- 区间不重叠由应用层在"每作品事务咨询锁 + OVERLAPS 检测"下强制（无需 contrib 扩展）
CREATE TABLE IF NOT EXISTS agreements (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_code  text NOT NULL REFERENCES works(code),
    valid_from timestamptz NOT NULL,
    valid_to   timestamptz,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agreements_range_chk
        CHECK (valid_to IS NULL OR valid_to > valid_from)
);
-- 作品 + 生效时间的匹配查询索引
CREATE INDEX IF NOT EXISTS agreements_work_from_idx
    ON agreements(work_code, valid_from);

CREATE TABLE IF NOT EXISTS agreement_shares (
    agreement_id bigint NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    holder_code  text NOT NULL REFERENCES rightsholders(code),
    percent      numeric(8,4) NOT NULL CHECK (percent > 0 AND percent <= 100),
    PRIMARY KEY (agreement_id, holder_code)
);

CREATE OR REPLACE FUNCTION check_shares_total() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    total numeric(12,4);
    aid   bigint;
BEGIN
    aid := COALESCE(NEW.agreement_id, OLD.agreement_id);
    SELECT sum(percent) INTO total FROM agreement_shares WHERE agreement_id = aid;
    IF total IS DISTINCT FROM 100.0000 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'agreement shares must total 100 percent, got '
                      || COALESCE(total::text, '0');
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS agreement_shares_total_trg ON agreement_shares;
-- 延迟约束触发器：允许先插协议、再逐条插分成，事务提交时校验合计 100%
CREATE CONSTRAINT TRIGGER agreement_shares_total_trg
    AFTER INSERT OR UPDATE OR DELETE ON agreement_shares
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_shares_total();

-- 播放记录：import_id 为调用方提供的唯一编号
CREATE TABLE IF NOT EXISTS plays (
    import_id   text PRIMARY KEY,
    work_code   text NOT NULL REFERENCES works(code),
    occurred_at timestamptz NOT NULL,
    amount      bigint NOT NULL CHECK (amount >= 0),
    corrects_id text UNIQUE REFERENCES plays(import_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT plays_no_self_corr CHECK (corrects_id IS NULL OR corrects_id <> import_id)
);
CREATE INDEX IF NOT EXISTS plays_occurred_idx ON plays(occurred_at);
-- corrects_id 自带 UNIQUE：同一记录最多被更正一次 => 禁止分叉

-- 结算单据
CREATE TABLE IF NOT EXISTS statement_batches (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_start         timestamptz NOT NULL,
    period_end           timestamptz NOT NULL,
    kind                 text NOT NULL CHECK (kind IN ('initial', 'supplement')),
    supersedes_batch_id  bigint REFERENCES statement_batches(id),
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT statement_batches_period_chk CHECK (period_end > period_start)
);
-- 同一期间只能有一张初次单（追补单数量不限）
CREATE UNIQUE INDEX IF NOT EXISTS statement_batches_initial_uniq
    ON statement_batches(period_start, period_end)
    WHERE kind = 'initial';

-- 每次出具时待处理播放的快照
CREATE TABLE IF NOT EXISTS statement_pending (
    period_start timestamptz NOT NULL,
    period_end   timestamptz NOT NULL,
    import_id    text NOT NULL REFERENCES plays(import_id),
    work_code    text NOT NULL,
    occurred_at  timestamptz NOT NULL,
    amount       bigint NOT NULL,
    reason       text NOT NULL,
    PRIMARY KEY (period_start, period_end, import_id)
);

-- 单据明细行：初次单为当期应付，追补单为差额（可正可负）
CREATE TABLE IF NOT EXISTS statement_lines (
    batch_id    bigint NOT NULL REFERENCES statement_batches(id) ON DELETE CASCADE,
    holder_code text NOT NULL,
    amount      bigint NOT NULL,
    PRIMARY KEY (batch_id, holder_code)
);
CREATE INDEX IF NOT EXISTS statement_lines_holder_idx ON statement_lines(holder_code);
"""


async def init_pool() -> None:
    global pool
    last_error = None
    # db 容器可能尚不可用（即使有 healthcheck 也容忍竞态），重试等待
    for attempt in range(30):
        try:
            p = ConnectionPool(
                conninfo=settings.database_url,
                min_size=settings.db_min_size,
                max_size=settings.db_max_size,
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=False,
            )
            p.open(wait=True)
            with p.connection() as conn:
                conn.execute("SELECT 1")
            pool = p
            with pool.connection() as conn:
                conn.execute(SCHEMA_SQL)
            return
        except Exception as exc:  # pragma: no cover - startup retry
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"cannot connect to database: {last_error}")


async def close_pool() -> None:
    if pool is not None:
        pool.close()


@asynccontextmanager
async def conn_cm():
    assert pool is not None
    with pool.connection() as conn:
        yield conn


def get_conn():
    """FastAPI 依赖：从连接池取连接（autocommit；写操作显式开事务）。

    连接上下文在退出时自动回滚未提交事务并归还连接池。
    """
    assert pool is not None
    with pool.connection() as conn:
        yield conn
