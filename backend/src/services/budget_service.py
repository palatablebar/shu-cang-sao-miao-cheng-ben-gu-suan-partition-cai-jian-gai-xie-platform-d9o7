"""BudgetPolicy service + 预算总览（月账单 vs 预算阈值）。

预算页只认「最近一次全量估算批次」的冻结快照（``estimate_run`` + ``estimate_run_item``）：
    original_cost_cny = 该批次 frozen 的单日扫描 × 30 天 × QUERY_VOLUME_SCALE
    confirmed_saving  = Σ 已采纳建议在该批次的冻结月节省
    pending_saving    = Σ 待采纳建议在该批次的冻结月节省
    adjusted_cost     = original − confirmed   （待确认节省不提前抵扣）
收益按建议归属批次计一次：日志重导或分区元数据变化不会改写历史批次，
同一批次重复执行或再次采纳也不会让收益翻倍。
"""
from sqlalchemy import Integer, case, func, select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants.cost import monthly_projection_cny
from src.constants.enums import BudgetStatus, SuggestionStatus
from src.constants.error_codes import AppError, ErrorCode
from src.constants.messages import MSG_POLICY_NOT_FOUND
from src.models import (
    BudgetPolicy,
    EstimateRun,
    EstimateRunItem,
    QueryLog,
    RewriteSuggestion,
    TableMeta,
)
from src.schemas.budget import (
    BudgetOverview,
    BudgetSavingRow,
    PolicyCreateRequest,
    PolicyOut,
    PolicyUpdateRequest,
    TableScanRow,
)
from src.services import serializers

GIB = 1024 ** 3

ADOPTED = SuggestionStatus.ADOPTED.value
PENDING = SuggestionStatus.PENDING.value


def list_policies(db: Session, active_only: bool = False) -> list[PolicyOut]:
    stmt = select(BudgetPolicy).order_by(BudgetPolicy.scope, BudgetPolicy.policy_no)
    if active_only:
        stmt = stmt.where(BudgetPolicy.is_active.is_(True))
    return [serializers.policy(row) for row in db.scalars(stmt)]


def get_policy(db: Session, policy_id: int) -> BudgetPolicy:
    policy = db.get(BudgetPolicy, policy_id)
    if policy is None:
        raise AppError(ErrorCode.NOT_FOUND, MSG_POLICY_NOT_FOUND)
    return policy


def _apply_payload(policy: BudgetPolicy, payload: PolicyUpdateRequest) -> None:
    if payload.name is not None:
        policy.name = payload.name
    if payload.monthly_budget_cny is not None:
        policy.monthly_budget_cny = payload.monthly_budget_cny
    if payload.unit_cost_per_gib_cny is not None:
        policy.unit_cost_per_gib_cny = payload.unit_cost_per_gib_cny
    if payload.warn_ratio is not None:
        policy.warn_ratio = payload.warn_ratio
    if payload.critical_ratio is not None:
        policy.critical_ratio = payload.critical_ratio
    if payload.scan_alert_gib is not None:
        policy.scan_alert_bytes = int(payload.scan_alert_gib * GIB)
    if payload.is_active is not None:
        policy.is_active = payload.is_active
    if payload.note is not None:
        policy.note = payload.note


def update_policy(db: Session, policy_id: int, payload: PolicyUpdateRequest, actor: str) -> PolicyOut:
    policy = get_policy(db, policy_id)
    _apply_payload(policy, payload)
    policy.updated_by = actor
    db.commit()
    db.refresh(policy)
    return serializers.policy(policy)


def create_policy(db: Session, payload: PolicyCreateRequest, actor: str) -> PolicyOut:
    existing = db.scalar(select(BudgetPolicy).where(BudgetPolicy.policy_no == payload.policy_no))
    if existing is not None:
        raise AppError(ErrorCode.CONFLICT, f"策略编号 {payload.policy_no} 已存在")
    policy = BudgetPolicy(
        policy_no=payload.policy_no,
        name=payload.name,
        scope=payload.scope,
        scope_value=payload.scope_value,
        monthly_budget_cny=payload.monthly_budget_cny,
        unit_cost_per_gib_cny=payload.unit_cost_per_gib_cny,
        warn_ratio=payload.warn_ratio,
        critical_ratio=payload.critical_ratio,
        scan_alert_bytes=int(payload.scan_alert_gib * GIB),
        note=payload.note,
        updated_by=actor,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return serializers.policy(policy)


def _status(projected: float, budget: float, warn: float, critical: float) -> str:
    if budget <= 0:
        return BudgetStatus.OK.value
    ratio = projected / budget
    if ratio > 1:
        return BudgetStatus.OVER.value
    if ratio >= critical:
        return BudgetStatus.CRITICAL.value
    if ratio >= warn:
        return BudgetStatus.WARN.value
    return BudgetStatus.OK.value


def _overview_tables(db: Session, run: EstimateRun | None) -> list[TableScanRow]:
    """按表分布优先取批次的冻结快照，日志重导后预算页也不会跳数。"""
    if run is not None:
        rows = db.execute(
            select(
                EstimateRunItem.target_table,
                func.coalesce(func.sum(EstimateRunItem.scan_bytes), 0),
                func.count(EstimateRunItem.id),
                func.coalesce(
                    func.sum(case((EstimateRunItem.prunable.is_(False), 1), else_=0)), 0
                ).cast(Integer),
            )
            .where(
                EstimateRunItem.estimate_run_id == run.id,
                EstimateRunItem.target_table.is_not(None),
            )
            .group_by(EstimateRunItem.target_table)
            .order_by(func.coalesce(func.sum(EstimateRunItem.scan_bytes), 0).desc())
        ).all()
    else:
        rows = db.execute(
            select(
                QueryLog.target_table,
                func.coalesce(func.sum(QueryLog.scan_bytes), 0),
                func.count(QueryLog.id),
                func.coalesce(func.sum(case((QueryLog.prunable.is_(False), 1), else_=0)), 0).cast(Integer),
            )
            .where(QueryLog.target_table.is_not(None))
            .group_by(QueryLog.target_table)
            .order_by(func.coalesce(func.sum(QueryLog.scan_bytes), 0).desc())
        ).all()
    layers = {table.table_name: table.layer for table in db.scalars(select(TableMeta))}
    return [
        TableScanRow(
            table_name=row[0],
            layer=layers.get(row[0], "DWD"),
            scan_bytes=int(row[1] or 0),
            query_count=int(row[2] or 0),
            blocked_count=int(row[3] or 0),
        )
        for row in rows
    ]


def _frozen_savings(db: Session) -> list[BudgetSavingRow]:
    """每条建议只取它所属批次的那一条冻结快照，因此跨批次汇总也不会重复累计。

    归属关系写在 ``RewriteSuggestion.estimate_run_id``：建议首次被计入哪个全量批次，
    就永远只在那个批次的快照里出现一次；即使之后又跑了新批次，这里也仍能看到旧批次
    冻结的收益（不会因为新批次而丢数，也不会因为再采纳而翻倍）。
    """
    rows = db.execute(
        select(EstimateRunItem, RewriteSuggestion, EstimateRun)
        .join(RewriteSuggestion, EstimateRunItem.suggestion_id == RewriteSuggestion.id)
        .join(EstimateRun, EstimateRunItem.estimate_run_id == EstimateRun.id)
        .where(
            RewriteSuggestion.estimate_run_id == EstimateRunItem.estimate_run_id,
            EstimateRunItem.suggestion_saving_bytes > 0,
            EstimateRun.status == "completed",
            EstimateRun.scope == "all",
        )
        .order_by(EstimateRunItem.suggestion_saving_bytes.desc(), EstimateRunItem.id.asc())
    ).all()
    result: list[BudgetSavingRow] = []
    for item, suggestion, run in rows:
        saving_cost = float(item.suggestion_saving_cost_cny)
        result.append(
            BudgetSavingRow(
                suggestion_no=suggestion.suggestion_no,
                query_external_id=item.external_id,
                target_table=item.target_table,
                status=suggestion.status,
                saving_bytes=int(item.suggestion_saving_bytes),
                saving_cost_cny=round(saving_cost, 6),
                monthly_saving_cny=monthly_projection_cny(
                    saving_cost, settings.MONTHLY_QUERY_DAYS, settings.QUERY_VOLUME_SCALE
                ),
                estimate_run_no=run.run_no,
            )
        )
    return result


def overview(db: Session) -> BudgetOverview:
    policy = db.scalar(
        select(BudgetPolicy)
        .where(BudgetPolicy.scope == "global", BudgetPolicy.is_active.is_(True))
        .order_by(BudgetPolicy.id)
        .limit(1)
    )
    budget = policy.monthly_budget_cny if policy else settings.DEFAULT_MONTHLY_BUDGET_CNY
    warn = policy.warn_ratio if policy else settings.DEFAULT_WARN_RATIO
    critical = policy.critical_ratio if policy else settings.DEFAULT_CRITICAL_RATIO
    unit_cost = policy.unit_cost_per_gib_cny if policy else settings.COST_PER_GIB_CNY
    alert_bytes = policy.scan_alert_bytes if policy else int(8 * GIB)

    # 预算总览只看全量批次：按表/按失效范围的专项批次会把总扫描量算小。
    latest_run = db.scalar(
        select(EstimateRun)
        .where(EstimateRun.status == "completed", EstimateRun.scope == "all")
        .order_by(EstimateRun.id.desc())
        .limit(1)
    )
    if latest_run is not None:
        total_scan = int(latest_run.total_scan_bytes)
        query_count = int(latest_run.query_count)
        blocked = int(latest_run.blocked_count)
        projected = float(latest_run.monthly_projected_cost_cny)
    else:
        # 还没跑过全量批次：用当前查询表兜底。
        total_scan = int(db.scalar(select(func.coalesce(func.sum(QueryLog.scan_bytes), 0))) or 0)
        query_count = int(db.scalar(select(func.count(QueryLog.id))) or 0)
        blocked = int(
            db.scalar(
                select(func.count(QueryLog.id)).where(QueryLog.prunable.is_(False), QueryLog.parse_status == "parsed")
            )
            or 0
        )
        projected = monthly_projection_cny(
            total_scan / GIB * unit_cost, settings.MONTHLY_QUERY_DAYS, settings.QUERY_VOLUME_SCALE
        )

    savings = _frozen_savings(db)
    confirmed = [row for row in savings if row.status == ADOPTED]
    pending = [row for row in savings if row.status == PENDING]
    confirmed_saving_cny = round(sum(row.monthly_saving_cny for row in confirmed), 2)
    pending_saving_cny = round(sum(row.monthly_saving_cny for row in pending), 2)
    adjusted = round(max(0.0, projected - confirmed_saving_cny), 2)
    saved_bytes = sum(row.saving_bytes for row in savings)
    suggestion_count = len(savings)

    return BudgetOverview(
        monthly_budget_cny=budget,
        original_cost_cny=round(projected, 2),
        confirmed_saving_cny=confirmed_saving_cny,
        pending_saving_cny=pending_saving_cny,
        adjusted_cost_cny=adjusted,
        projected_cost_cny=adjusted,
        month_share=round(adjusted / budget, 4) if budget else 0.0,
        status=_status(adjusted, budget, warn, critical),
        confirmed_share=round(confirmed_saving_cny / budget, 4) if budget else 0.0,
        pending_share=round(pending_saving_cny / budget, 4) if budget else 0.0,
        unit_cost_per_gib_cny=unit_cost,
        alert_bytes=alert_bytes,
        total_scan_bytes=total_scan,
        query_count=query_count,
        blocked_count=blocked,
        estimate_run_no=latest_run.run_no if latest_run else None,
        estimate_run_finished_at=(
            latest_run.finished_at.isoformat() if latest_run and latest_run.finished_at else None
        ),
        prunable_saving_bytes=saved_bytes,
        suggestion_count=suggestion_count,
        confirmed_suggestion_count=len(confirmed),
        pending_suggestion_count=len(pending),
        savings=savings,
        tables=_overview_tables(db, latest_run),
        policies=list_policies(db),
    )
