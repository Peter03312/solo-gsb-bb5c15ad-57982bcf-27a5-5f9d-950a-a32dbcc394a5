from fastapi import APIRouter, Depends, Query

from .. import statements_service as svc
from ..db import get_conn
from ..schemas import AttributionPage, PeriodParams, StatementOut, StatementSummary

router = APIRouter(prefix="/statements", tags=["statements"])


@router.post("/issue", response_model=StatementOut)
def issue(period: PeriodParams, conn=Depends(get_conn)):
    """按期间出具单据。

    - 首次：出具初次单（已出单据不可覆盖）；
    - 再次：按最新应付额与此期间全部单据累计额之差出具追补单，差额可正可负；
    - 应付无变化时不产生新单据，也不生成归因证据；
    - 同一期间并发出具由咨询锁串行化，不会产生重复单据或重复证据；
    - 出具时按更正链原始编号冻结逐笔归因（链尾、作品、发生时间、匹配协议、
      逐笔分摊），历史快照不随日后数据变化；
    - 存量无证据旧单保持不可追溯；其后首张追补单把无法归因的旧账差额单列。
    """
    return svc.issue_statement(conn, period.start, period.end)


@router.get("/preview")
def preview(period: PeriodParams = Depends(), conn=Depends(get_conn)):
    """不出具，仅试算当前应付额、待处理清单及若追补将产生的差额。"""
    return svc.preview_period(conn, period.start, period.end)


@router.get("", response_model=list[StatementSummary])
def list_statements(period: PeriodParams = Depends(), conn=Depends(get_conn)):
    """列出某期间的全部单据（初次单 + 历次追补单，按出具顺序）。"""
    return svc.list_batches(conn, period.start, period.end)


@router.get("/{batch_id}/attribution", response_model=AttributionPage)
def get_attribution(
    batch_id: int,
    holder_code: str | None = Query(default=None, min_length=1, max_length=200),
    page: int = Query(default=1),
    page_size: int = Query(default=50),
    conn=Depends(get_conn),
):
    """按单据分页查询逐笔归因记录与权利人合计。

    - 按更正链**原始编号**归集，每组给出具时冻结的链尾编号、作品、发生时间、
      匹配协议及逐笔分摊；追补单标注 added/revoked，负额不丢失；
    - `holder_totals` 按权利人汇总，与该单据原有明细行严格一致；
    - 可加 `holder_code` 只看某一权利人；`page` 从 1 开始，
      `page_size` 范围 1–200，非法参数返回 422；
    - 不存在的单据返回 404；存量无证据单据返回 traceable=false 并注明不可追溯。
    """
    return svc.list_attribution(conn, batch_id, holder_code, page, page_size)
