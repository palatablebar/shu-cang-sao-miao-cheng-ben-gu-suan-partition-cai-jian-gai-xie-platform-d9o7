"""BudgetPolicy schemas + 预算总览。

预算口径（README §算法 3）：
    original_cost_cny        最近一次全量批次冻结的月折算原费用（算法 1 改写前）
    confirmed_saving_cny     已采纳建议在该批次冻结的月节省
    pending_saving_cny       待采纳建议在该批次冻结的月节省
    adjusted_cost_cny        original − confirmed（待确认节省只做参考，不提前抵扣）
"""
from pydantic import BaseModel, Field


class PolicyOut(BaseModel):
    id: int
    policy_no: str
    name: str
    scope: str
    scope_value: str | None
    monthly_budget_cny: float
    unit_cost_per_gib_cny: float
    warn_ratio: float
    critical_ratio: float
    scan_alert_bytes: int
    is_active: bool
    note: str
    updated_by: str | None


class PolicyCreateRequest(BaseModel):
    policy_no: str = Field(min_length=3, max_length=32)
    name: str = Field(min_length=2, max_length=96)
    scope: str = Field(default="global", pattern="^(global|layer|table)$")
    scope_value: str | None = None
    monthly_budget_cny: float = Field(gt=0)
    unit_cost_per_gib_cny: float = Field(default=0.30, gt=0)
    warn_ratio: float = Field(default=0.75, gt=0, le=1)
    critical_ratio: float = Field(default=0.92, gt=0, le=1)
    scan_alert_gib: float = Field(default=8.0, gt=0)
    note: str = ""


class PolicyUpdateRequest(BaseModel):
    name: str | None = None
    monthly_budget_cny: float | None = Field(default=None, gt=0)
    unit_cost_per_gib_cny: float | None = Field(default=None, gt=0)
    warn_ratio: float | None = Field(default=None, gt=0, le=1)
    critical_ratio: float | None = Field(default=None, gt=0, le=1)
    scan_alert_gib: float | None = Field(default=None, gt=0)
    is_active: bool | None = None
    note: str | None = None


class TableScanRow(BaseModel):
    table_name: str
    layer: str
    scan_bytes: int
    query_count: int
    blocked_count: int


class BudgetSavingRow(BaseModel):
    """一条建议在预算口径里的贡献（数值取自所属批次的冻结快照）。"""

    suggestion_no: str
    query_external_id: str
    target_table: str | None
    status: str
    saving_bytes: int
    saving_cost_cny: float
    monthly_saving_cny: float
    estimate_run_no: str | None


class BudgetOverview(BaseModel):
    monthly_budget_cny: float
    # --- 本月费用对照（全部来自最近一次全量批次的冻结快照）---
    original_cost_cny: float
    confirmed_saving_cny: float
    pending_saving_cny: float
    adjusted_cost_cny: float
    # 兼容旧字段名：projected_cost_cny = 调整后预计费用，month_share 按调整后费用计算
    projected_cost_cny: float
    month_share: float
    status: str
    confirmed_share: float
    pending_share: float
    unit_cost_per_gib_cny: float
    alert_bytes: int
    total_scan_bytes: int
    query_count: int
    blocked_count: int
    estimate_run_no: str | None
    estimate_run_finished_at: str | None
    # 冻结口径：该批次计入的建议收益（每条建议只计一次）
    prunable_saving_bytes: int
    suggestion_count: int
    confirmed_suggestion_count: int
    pending_suggestion_count: int
    savings: list[BudgetSavingRow]
    tables: list[TableScanRow]
    policies: list[PolicyOut]
