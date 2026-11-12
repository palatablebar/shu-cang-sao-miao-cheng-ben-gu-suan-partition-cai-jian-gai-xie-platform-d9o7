"""EstimateRun service: create → (Celery|inline) 执行 → 冻结快照 → 预算判定.

一次批次的结果全部写进 ``estimate_run_item`` 快照：
    * 扫描量、命中分区、裁剪结论在批次执行时冻结，之后重导日志或改分区元数据都不会改写历史批次；
    * 建议收益也冻结在快照里，并且每条建议只在首次计入它的批次出现一次；
    * 整个批次的写入在同一个事务里完成，失败回滚后不会留下半批数据，重试只会重建这一批。
"""
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.analysis.estimator import EstimateRow, summarize_rows
from src.analysis.rewriter import RewriteResult, build_rewrite
from src.config.settings import settings
from src.constants.enums import BudgetStatus, RunStatus
from src.constants.error_codes import AppError, ErrorCode
from src.constants.messages import MSG_ESTIMATE_NOT_FOUND, MSG_TABLE_NOT_FOUND
from src.models import BudgetPolicy, EstimateRun, EstimateRunItem, QueryLog, RewriteSuggestion, TableMeta
from src.schemas.estimate import EstimateCreateRequest, EstimateRunOut
from src.services import rewrite_service, serializers, table_service
from src.utils.logger import log_event
from src.utils.pagination import PageParams

GIB = 1024 ** 3


def next_run_no(db: Session) -> str:
    prefix = datetime.now(timezone.utc).strftime("EST-%Y%m%d")
    used = db.scalar(select(func.count(EstimateRun.id)).where(EstimateRun.run_no.like(f"{prefix}-%"))) or 0
    return f"{prefix}-{int(used) + 1:02d}"


def budget_status_for(projected_cost: float, budget: float, warn: float, critical: float) -> str:
    if budget <= 0:
        return BudgetStatus.OK.value
    ratio = projected_cost / budget
    if ratio > 1:
        return BudgetStatus.OVER.value
    if ratio >= critical:
        return BudgetStatus.CRITICAL.value
    if ratio >= warn:
        return BudgetStatus.WARN.value
    return BudgetStatus.OK.value


def _global_policy(db: Session) -> BudgetPolicy | None:
    return db.scalar(
        select(BudgetPolicy)
        .where(BudgetPolicy.scope == "global", BudgetPolicy.is_active.is_(True))
        .order_by(BudgetPolicy.id)
        .limit(1)
    )


def create_run(db: Session, actor: str, payload: EstimateCreateRequest) -> EstimateRun:
    if payload.scope == "table":
        if not payload.scope_value:
            raise AppError(ErrorCode.BAD_REQUEST, "按表估算必须指定表名")
        table = db.scalar(select(TableMeta).where(TableMeta.table_name == payload.scope_value))
        if table is None:
            raise AppError(ErrorCode.NOT_FOUND, MSG_TABLE_NOT_FOUND)
    run = EstimateRun(
        run_no=next_run_no(db),
        status=RunStatus.QUEUED.value,
        scope=payload.scope,
        scope_value=payload.scope_value,
        note=payload.note,
        created_by=actor,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    dispatch(run.id)
    return run


def dispatch(run_id: int) -> str:
    """Send the run to Celery; fall back to inline execution if the broker is down."""
    if settings.ESTIMATE_INLINE:
        from src.config.database import SessionLocal

        with SessionLocal() as db:
            execute_run(db, run_id)
        return "inline"
    try:
        from src.tasks.estimate_task import run_estimate_task

        run_estimate_task.delay(run_id)
        return "celery"
    except Exception as exc:  # pragma: no cover - broker failure path
        log_event("ESTIMATE_DISPATCH_FALLBACK", run_id=run_id, error=f"{type(exc).__name__}: {exc}")
        from src.config.database import SessionLocal

        with SessionLocal() as db:
            execute_run(db, run_id)
        return "inline"


def _scope_queries(db: Session, run: EstimateRun) -> list[QueryLog]:
    """Deterministic scan list: same scope → same fixed sample (ordered by id)."""
    stmt = select(QueryLog).order_by(QueryLog.id)
    if run.scope == "table":
        stmt = stmt.where(QueryLog.target_table == (run.scope_value or ""))
    elif run.scope == "blocked":
        stmt = stmt.where(QueryLog.prunable.is_(False), QueryLog.parse_status == "parsed")
    return list(db.scalars(stmt.limit(settings.ESTIMATE_MAX_QUERIES)))


def _rewrite_result(query: QueryLog, catalog) -> RewriteResult:
    return build_rewrite(
        query.sql_text,
        catalog,
        window_days=settings.REWRITE_WINDOW_DAYS,
        cost_per_gib_cny=settings.COST_PER_GIB_CNY,
        risk_weights=rewrite_service.RISK_WEIGHTS,
    )


def _ensure_suggestion(
    db: Session,
    query: QueryLog,
    result: RewriteResult,
    run: EstimateRun,
) -> tuple[RewriteSuggestion | None, bool]:
    """Return (suggestion, counted_in_this_run).

    建议收益只在首次计入的全量批次（``RewriteSuggestion.estimate_run_id``）出现一次：
    * 已有建议但尚未归属批次 → 在全量批次计入并写死归属；
    * 已归属别的批次 → 只引用、不计收益，也不改写那个批次的历史数字；
    * 同一批次失败重试/重复执行 → 幂等刷新本批次自己的那一条。

    按表 / 按失效范围的专项批次只冻结扫描结论，不重复计入建议收益，避免同一收益
    在不同批次里被算第二次（预算口径只读全量批次）。
    """
    if run.scope != "all":
        return None, False
    suggestion = db.scalar(
        select(RewriteSuggestion).where(
            RewriteSuggestion.suggestion_no == rewrite_service.suggestion_no_for(query)
        )
    )
    if suggestion is None or not result.rewritable:
        return suggestion, False
    if suggestion.estimate_run_id is not None and suggestion.estimate_run_id != run.id:
        return suggestion, False
    if suggestion.estimate_run_id is None:
        suggestion.estimate_run_id = run.id
    return suggestion, True


def _build_rows(db: Session, run: EstimateRun, queries: list[QueryLog], catalog) -> list[EstimateRow]:
    """Analyse every query once; rewrite savings come from the query's already-attributed suggestion."""
    from src.services.query_service import _apply_analysis

    rows: list[EstimateRow] = []
    for query in queries:
        result = _rewrite_result(query, catalog)
        _apply_analysis(query, result.before, settings.COST_PER_GIB_CNY)
        suggestion, counted = _ensure_suggestion(db, query, result, run)
        rows.append(
            EstimateRow(
                query_id=query.id,
                analysis=result.before,
                rewrite_saving_bytes=result.saving_bytes if counted else 0,
                rewrite_saving_cost_cny=result.saving_cost_cny if counted else 0.0,
                suggestion_id=suggestion.id if counted and suggestion is not None else None,
            )
        )
    return rows


def _freeze_items(db: Session, run: EstimateRun, summary, queries: list[QueryLog]) -> None:
    """Replace this run's snapshot atomically (delete + insert in the caller's transaction)."""
    db.execute(delete(EstimateRunItem).where(EstimateRunItem.estimate_run_id == run.id))
    by_id = {query.id: query for query in queries}
    for row in summary.rows:
        analysis = row.analysis
        query = by_id.get(row.query_id)
        db.add(
            EstimateRunItem(
                estimate_run_id=run.id,
                query_log_id=row.query_id,
                external_id=query.external_id if query else "",
                target_table=analysis.target_table,
                parse_status=analysis.parse_status,
                prunable=analysis.prunable,
                blocked_reason=analysis.blocked_reason,
                hit_partition_count=analysis.hit_partition_count,
                total_partition_count=analysis.total_partition_count,
                scan_bytes=analysis.scan_bytes,
                scan_cost_cny=round(analysis.scan_bytes / GIB * settings.COST_PER_GIB_CNY, 6),
                suggestion_id=row.suggestion_id,
                suggestion_saving_bytes=row.rewrite_saving_bytes,
                suggestion_saving_cost_cny=row.rewrite_saving_cost_cny,
            )
        )
    db.flush()


def _apply_run_summary(run: EstimateRun, summary, policy: BudgetPolicy | None) -> None:
    budget = policy.monthly_budget_cny if policy else settings.DEFAULT_MONTHLY_BUDGET_CNY
    warn = policy.warn_ratio if policy else settings.DEFAULT_WARN_RATIO
    critical = policy.critical_ratio if policy else settings.DEFAULT_CRITICAL_RATIO
    run.status = RunStatus.COMPLETED.value
    run.query_count = summary.query_count
    run.parsed_count = summary.parsed_count
    run.blocked_count = summary.blocked_count
    run.prunable_count = summary.prunable_count
    run.total_scan_bytes = summary.total_scan_bytes
    run.total_cost_cny = summary.total_cost_cny
    run.monthly_projected_cost_cny = summary.monthly_projected_cost_cny
    run.saved_by_rewrites_bytes = summary.saved_by_rewrites_bytes
    run.saved_by_rewrites_cost_cny = summary.saved_by_rewrites_cost_cny
    run.suggestion_count = summary.suggestion_count
    run.top_query_id = summary.top_query_id
    run.top_scan_bytes = summary.top_scan_bytes
    run.budget_policy_id = policy.id if policy else None
    run.budget_status = budget_status_for(summary.monthly_projected_cost_cny, budget, warn, critical)
    run.finished_at = datetime.now(timezone.utc)


def execute_run(db: Session, run_id: int) -> EstimateRun:
    run = db.scalar(select(EstimateRun).where(EstimateRun.id == run_id).with_for_update())
    if run is None:
        raise AppError(ErrorCode.NOT_FOUND, MSG_ESTIMATE_NOT_FOUND)
    if run.status == RunStatus.COMPLETED.value:
        return run

    run.status = RunStatus.RUNNING.value
    run.started_at = datetime.now(timezone.utc)
    run.error_message = None
    db.commit()

    try:
        queries = _scope_queries(db, run)
        catalog = table_service.build_catalog(db)

        # 算法 1 + 算法 2 全部在内存里算完，最后一次性落库（失败时整批回滚）。
        rows = _build_rows(db, run, queries, catalog)
        summary = summarize_rows(
            rows,
            settings.COST_PER_GIB_CNY,
            settings.MONTHLY_QUERY_DAYS,
            settings.QUERY_VOLUME_SCALE,
        )
        _freeze_items(db, run, summary, queries)
        _apply_run_summary(run, summary, _global_policy(db))
        db.commit()
        log_event(
            "ESTIMATE_RUN_COMPLETED",
            run_no=run.run_no,
            queries=run.query_count,
            blocked=run.blocked_count,
            total_scan_bytes=run.total_scan_bytes,
            saved_by_rewrites_bytes=run.saved_by_rewrites_bytes,
        )
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(EstimateRun, run_id)
        if run is not None:
            # 失败批次不保留任何快照：半批数据绝不能进入预算口径。
            db.execute(delete(EstimateRunItem).where(EstimateRunItem.estimate_run_id == run_id))
            run.status = RunStatus.FAILED.value
            run.error_message = f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        log_event("ESTIMATE_RUN_FAILED", run_id=run_id, error=f"{type(exc).__name__}: {exc}")
        raise


def mark_failed(db: Session, run_id: int, message: str) -> None:
    run = db.get(EstimateRun, run_id)
    if run is None or run.status == RunStatus.COMPLETED.value:
        return
    db.execute(delete(EstimateRunItem).where(EstimateRunItem.estimate_run_id == run_id))
    run.status = RunStatus.FAILED.value
    run.error_message = message[:900]
    run.finished_at = datetime.now(timezone.utc)
    db.commit()


def get_run(db: Session, run_id: int) -> EstimateRun:
    run = db.get(EstimateRun, run_id)
    if run is None:
        raise AppError(ErrorCode.NOT_FOUND, MSG_ESTIMATE_NOT_FOUND)
    return run


def run_detail(db: Session, run_id: int) -> EstimateRunOut:
    return serializers.estimate_run(get_run(db, run_id))


def list_runs(db: Session, params: PageParams) -> tuple[list[EstimateRunOut], int]:
    total = int(db.scalar(select(func.count(EstimateRun.id))) or 0)
    rows = db.scalars(
        select(EstimateRun).order_by(EstimateRun.id.desc()).offset(params.offset).limit(params.limit)
    )
    return [serializers.estimate_run(row) for row in rows], total
