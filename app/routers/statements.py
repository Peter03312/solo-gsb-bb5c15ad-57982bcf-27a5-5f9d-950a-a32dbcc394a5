from fastapi import APIRouter, Depends

from .. import statements_service as svc
from ..db import get_conn
from ..schemas import PeriodParams, StatementOut, StatementSummary

router = APIRouter(prefix="/statements", tags=["statements"])


@router.post("/issue", response_model=StatementOut)
def issue(period: PeriodParams, conn=Depends(get_conn)):
    """按期间出具单据。

    - 首次：出具初次单（已出单据不可覆盖）；
    - 再次：按最新应付额与此期间全部单据累计额之差出具追补单，差额可正可负；
    - 应付无变化时不产生新单据；
    - 同一期间并发出具由咨询锁串行化，不会产生重复单据。
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
