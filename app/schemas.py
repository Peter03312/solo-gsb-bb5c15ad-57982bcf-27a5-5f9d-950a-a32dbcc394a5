from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import settings


def _tz_aware(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{field} must include a timezone, e.g. 2026-01-15T20:00:00+08:00")
    return dt


class WorkIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)


class WorkOut(BaseModel):
    code: str
    title: str
    created: bool


class HolderIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=500)


class HolderOut(BaseModel):
    code: str
    name: str | None
    created: bool


class ShareIn(BaseModel):
    # 用字符串接收，避免浮点误差，如 "62.5"；数字也兼容
    holder_code: str = Field(min_length=1, max_length=200)
    percent: Decimal = Field(gt=0, le=100)

    @field_validator("percent")
    @classmethod
    def scale_limit(cls, v: Decimal) -> Decimal:
        exp = v.as_tuple().exponent
        digits = -exp if isinstance(exp, int) and exp < 0 else 0
        if digits > settings.percent_scale:
            raise ValueError(
                f"percent supports at most {settings.percent_scale} decimal places"
            )
        return v


class AgreementIn(BaseModel):
    work_code: str = Field(min_length=1)
    valid_from: datetime
    valid_to: datetime | None = None
    note: str | None = None
    shares: list[ShareIn] = Field(min_length=1)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def tz(cls, v: datetime | None, info):
        if v is not None:
            _tz_aware(v, info.field_name)
        return v

    @model_validator(mode="after")
    def validate_model(self):
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        holder_codes = [s.holder_code for s in self.shares]
        if len(set(holder_codes)) != len(holder_codes):
            raise ValueError("duplicate holder_code in shares")
        total = sum((s.percent for s in self.shares), Decimal(0))
        if total != Decimal(100):
            raise ValueError(f"shares percent must total exactly 100, got {total}")
        return self


class AgreementShareOut(BaseModel):
    holder_code: str
    percent: str


class AgreementOut(BaseModel):
    id: int
    work_code: str
    valid_from: datetime
    valid_to: datetime | None
    note: str | None
    shares: list[AgreementShareOut]


class PlayIn(BaseModel):
    import_id: str = Field(min_length=1, max_length=200)
    work_code: str = Field(min_length=1)
    occurred_at: datetime
    # 非负整数，最小货币单位
    amount: int = Field(ge=0)
    # 更正时引用原记录编号，本记录必须使用新编号
    corrects_id: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v

    @field_validator("occurred_at")
    @classmethod
    def tz(cls, v: datetime):
        return _tz_aware(v, "occurred_at")

    @model_validator(mode="after")
    def validate_model(self):
        if self.corrects_id is not None and self.corrects_id == self.import_id:
            raise ValueError("corrects_id must differ from import_id")
        return self


class PlayBulkIn(BaseModel):
    plays: list[PlayIn] = Field(min_length=1, max_length=1000)


class PlayResult(BaseModel):
    import_id: str
    status: str  # created | duplicate
    is_correction: bool
    corrects_id: str | None
    chain_tail: bool


class PlayChainItem(BaseModel):
    import_id: str
    work_code: str
    occurred_at: datetime
    amount: int
    corrects_id: str | None
    chain_tail: bool


class PlayChainOut(BaseModel):
    import_id: str
    chain: list[PlayChainItem]


class PeriodParams(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def tz(cls, v: datetime, info):
        return _tz_aware(v, info.field_name)

    @model_validator(mode="after")
    def validate_model(self):
        if self.end <= self.start:
            raise ValueError("end must be later than start")
        return self


class PendingItem(BaseModel):
    import_id: str
    work_code: str
    occurred_at: datetime
    amount: int
    reason: str


class StatementLineOut(BaseModel):
    holder_code: str
    amount: int
    delta: int | None = None


class StatementOut(BaseModel):
    batch_id: int
    kind: str
    supersedes_batch_id: int | None
    period_start: datetime
    period_end: datetime
    issued_at: datetime
    note: str | None = None
    payable: list[StatementLineOut]
    pending: list[PendingItem]


class StatementSummary(BaseModel):
    batch_id: int
    kind: str
    supersedes_batch_id: int | None
    created_at: datetime
    lines: list[StatementLineOut]
    pending: list[PendingItem]
