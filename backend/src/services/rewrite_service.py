"""RewriteSuggestion service：算法 2 的落库、重估、采纳与 CSV 导出。"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.analysis.rewriter import RewriteResult, build_rewrite
from src.config.settings import settings
from src.constants.enums import SuggestionStatus
from src.constants.error_codes import AppError, ErrorCode
from src.constants.cost import GIB as _GIB, monthly_projection_cny
from src.constants.messages import MSG_QUERY_NOT_FOUND, MSG_SUGGESTION_NOT_FOUND
from src.models import EstimateRun, QueryLog, RewriteSuggestion
from src.schemas.rewrite import RewriteDetail, RewriteOut, RewriteReevaluateOut
from src.services import serializers, table_service
from src.utils.csv_export import suggestions_csv
from src.utils.pagination import PageParams

GIB = 1024 ** 3

RISK_WEIGHTS = {
    "low": settings.RISK_WEIGHT_LOW,
    "medium": settings.RISK_WEIGHT_MEDIUM,
    "high": settings.RISK_WEIGHT_HIGH,
}


def suggestion_no_for(query: QueryLog) -> str:
    return f"RW-{query.external_id}"


def run_algorithm(db: Session, query: QueryLog, catalog=None) -> RewriteResult:
    catalog = catalog if catalog is not None else table_service.build_catalog(db)
    return build_rewrite(
        query.sql_text,
        catalog,
        window_days=settings.REWRITE_WINDOW_DAYS,
        cost_per_gib_cny=settings.COST_PER_GIB_CNY,
        risk_weights=RISK_WEIGHTS,
    )


def _apply(suggestion: RewriteSuggestion, query: QueryLog, result: RewriteResult) -> None:
    suggestion.query_log_id = query.id
    suggestion.anti_patterns = result.anti_patterns
    suggestion.risk_level = result.risk_level
    suggestion.confidence = result.confidence
    suggestion.before_scan_bytes = result.before_scan_bytes
    suggestion.after_scan_bytes = result.after_scan_bytes
    suggestion.saving_bytes = result.saving_bytes
    suggestion.saving_cost_cny = result.saving_cost_cny
    suggestion.saving_pct = result.saving_pct
    suggestion.score = result.score
    suggestion.rewritten_sql = result.rewritten_sql
    suggestion.rationale = result.rationale


def get_suggestion(db: Session, suggestion_id: int) -> RewriteSuggestion:
    suggestion = db.scalar(
        select(RewriteSuggestion)
        .options(selectinload(RewriteSuggestion.query))
        .where(RewriteSuggestion.id == suggestion_id)
    )
    if suggestion is None:
        raise AppError(ErrorCode.NOT_FOUND, MSG_SUGGESTION_NOT_FOUND)
    return suggestion


def detail(db: Session, suggestion_id: int) -> RewriteDetail:
    suggestion = get_suggestion(db, suggestion_id)
    query = suggestion.query
    result = run_algorithm(db, query)
    extra = {
        "fixes": result.fixes,
        "before_rationale": result.before.rationale if result.before else "",
        "after_rationale": result.after.rationale if result.after else "",
        "after_prunable": bool(result.after and result.after.prunable),
        "after_hit_partition_count": result.after.hit_partition_count if result.after else 0,
        "after_total_partition_count": result.after.total_partition_count if result.after else 0,
    }
    return serializers.rewrite_detail(suggestion, extra)


def generate_for_query(db: Session, query_id: int, actor: str) -> RewriteDetail:
    query = db.get(QueryLog, query_id)
    if query is None:
        raise AppError(ErrorCode.NOT_FOUND, MSG_QUERY_NOT_FOUND)
    catalog = table_service.build_catalog(db)
    result = run_algorithm(db, query, catalog)
    if not result.rewritable:
        raise AppError(
            ErrorCode.CONFLICT,
            f"{query.external_id} 当前命中不到可改写项：{result.note or '节省比例不足 5%'}",
            {"anti_patterns": result.anti_patterns, "saving_pct": result.saving_pct},
        )
    suggestion = db.scalar(
        select(RewriteSuggestion).where(RewriteSuggestion.suggestion_no == suggestion_no_for(query))
    )
    if suggestion is None:
        suggestion = RewriteSuggestion(
            suggestion_no=suggestion_no_for(query),
            query_log_id=query.id,
            status=SuggestionStatus.PENDING.value,
        )
        db.add(suggestion)
    else:
        suggestion.reviewed_by = None
        suggestion.reviewed_at = None
    _apply(suggestion, query, result)
    suggestion.updated_at = datetime.now(timezone.utc)
    _attribute_to_latest_run(db, suggestion)
    db.commit()
    db.refresh(suggestion)
    return detail(db, suggestion.id)


def rebuild_suggestions(db: Session, limit: int = 12) -> int:
    """幂等：按分数取前 limit 条写入（自然键 suggestion_no），已存在的不覆盖。"""
    catalog = table_service.build_catalog(db)
    queries = list(
        db.scalars(select(QueryLog).where(QueryLog.prunable.is_(False), QueryLog.parse_status == "parsed"))
    )
    scored = []
    for query in queries:
        result = run_algorithm(db, query, catalog)
        if result.rewritable:
            scored.append((query, result))
    scored.sort(key=lambda item: (-item[1].score, item[0].external_id))
    created = 0
    for query, result in scored[:limit]:
        key = suggestion_no_for(query)
        existing = db.scalar(select(RewriteSuggestion).where(RewriteSuggestion.suggestion_no == key))
        if existing is not None:
            # 幂等补齐：历史库里未归属批次的建议只补归属，不覆盖已有数值。
            _attribute_to_latest_run(db, existing)
            continue
        suggestion = RewriteSuggestion(
            suggestion_no=key,
            query_log_id=query.id,
            status=SuggestionStatus.PENDING.value,
        )
        _apply(suggestion, query, result)
        db.add(suggestion)
        db.flush()
        # 若已有全量批次冻结过这条查询，收益就计入该批次一次，预算页可见「待确认节省」。
        _attribute_to_latest_run(db, suggestion)
        created += 1
    db.commit()
    return created


def top_suggestions(db: Session, limit: int = 3) -> list[RewriteOut]:
    return list_suggestions(db, PageParams(page=1, page_size=limit))[0]


def list_suggestions(
    db: Session,
    params: PageParams,
    status: str | None = None,
    sort: str = "score",
) -> tuple[list[RewriteOut], int]:
    stmt = select(RewriteSuggestion).options(selectinload(RewriteSuggestion.query))
    count_stmt = select(func.count(RewriteSuggestion.id))
    if status:
        stmt = stmt.where(RewriteSuggestion.status == status)
        count_stmt = count_stmt.where(RewriteSuggestion.status == status)
    column = {
        "score": RewriteSuggestion.score,
        "saving_bytes": RewriteSuggestion.saving_bytes,
        "saving_pct": RewriteSuggestion.saving_pct,
    }.get(sort, RewriteSuggestion.score)
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(
        stmt.order_by(column.desc(), RewriteSuggestion.id.asc()).offset(params.offset).limit(params.limit)
    )
    return [serializers.rewrite_item(row) for row in rows], total


def reevaluate(db: Session, suggestion_id: int) -> RewriteReevaluateOut:
    """对改写后的 SQL 重跑算法 1，用真实重估替代拍脑袋的收益数字。

    重估结果会同步回建议所属批次的那一条快照（若有），不会在别的批次重复计一次收益。
    """
    suggestion = get_suggestion(db, suggestion_id)
    query = suggestion.query
    catalog = table_service.build_catalog(db)
    result = run_algorithm(db, query, catalog)
    if not result.rewritable:
        raise AppError(ErrorCode.CONFLICT, f"重估失败：{result.note or '改写后不再命中反模式'}")
    _apply(suggestion, query, result)
    suggestion.updated_at = datetime.now(timezone.utc)
    _sync_item_snapshot(db, suggestion)
    _refresh_run_totals(db, suggestion.estimate_run_id)
    db.commit()
    return RewriteReevaluateOut(
        suggestion_no=suggestion.suggestion_no,
        before_scan_bytes=result.before_scan_bytes,
        after_scan_bytes=result.after_scan_bytes,
        saving_bytes=result.saving_bytes,
        saving_pct=result.saving_pct,
        saving_cost_cny=result.saving_cost_cny,
        monthly_saving_cny=monthly_projection_cny(
            result.saving_cost_cny, settings.MONTHLY_QUERY_DAYS, settings.QUERY_VOLUME_SCALE
        ),
        after_prunable=bool(result.after and result.after.prunable),
        after_hit_partition_count=result.after.hit_partition_count if result.after else 0,
        after_total_partition_count=result.after.total_partition_count if result.after else 0,
        rationale=result.rationale,
    )


def _attribute_to_latest_run(db: Session, suggestion: RewriteSuggestion) -> None:
    """把一条尚未归属批次的建议计入最近覆盖它的已完成批次（收益只在这里计一次）。

    生成改写后如果已有批次冻结过这条查询，预算页应立刻能看到「待确认节省」；
    若没有对应批次，建议保持未归属，等下一次估算批次计入。
    """
    if suggestion.estimate_run_id is not None:
        return
    from src.models import EstimateRun, EstimateRunItem

    applies = suggestion.anti_patterns or []
    if not applies or suggestion.saving_bytes <= 0:
        return
    item = db.scalar(
        select(EstimateRunItem)
        .join(EstimateRun, EstimateRunItem.estimate_run_id == EstimateRun.id)
        .where(
            EstimateRunItem.query_log_id == suggestion.query_log_id,
            EstimateRunItem.suggestion_id.is_(None),
            EstimateRun.status == "completed",
            EstimateRun.scope == "all",
        )
        .order_by(EstimateRun.id.desc())
        .limit(1)
    )
    if item is None:
        return
    suggestion.estimate_run_id = item.estimate_run_id
    item.suggestion_id = suggestion.id
    item.suggestion_saving_bytes = suggestion.saving_bytes
    item.suggestion_saving_cost_cny = suggestion.saving_cost_cny
    _refresh_run_totals(db, item.estimate_run_id)


def _sync_item_snapshot(db: Session, suggestion: RewriteSuggestion) -> None:
    """Keep the owning batch's frozen item in sync with a re-evaluated suggestion.

    只在建议所属批次内更新那一行，别的批次不会因此多出一条收益；
    未归属任何批次的建议（例如单独生成后还没跑估算）不产生批次计入。
    """
    if suggestion.estimate_run_id is None:
        return
    from src.models import EstimateRunItem

    item = db.scalar(
        select(EstimateRunItem).where(
            EstimateRunItem.estimate_run_id == suggestion.estimate_run_id,
            EstimateRunItem.query_log_id == suggestion.query_log_id,
        )
    )
    if item is None:
        return
    item.suggestion_id = suggestion.id
    item.suggestion_saving_bytes = suggestion.saving_bytes
    item.suggestion_saving_cost_cny = suggestion.saving_cost_cny


def _refresh_run_totals(db: Session, run_id: int | None) -> None:
    if run_id is None:
        return
    from src.models import EstimateRun, EstimateRunItem

    run = db.get(EstimateRun, run_id)
    if run is None:
        return
    row = db.execute(
        select(
            func.coalesce(func.sum(EstimateRunItem.suggestion_saving_bytes), 0),
            func.coalesce(func.sum(EstimateRunItem.suggestion_saving_cost_cny), 0.0),
            func.count(EstimateRunItem.suggestion_id),
        ).where(
            EstimateRunItem.estimate_run_id == run.id,
            EstimateRunItem.suggestion_saving_bytes > 0,
        )
    ).one()
    run.saved_by_rewrites_bytes = int(row[0] or 0)
    run.saved_by_rewrites_cost_cny = round(float(row[1] or 0), 6)
    run.suggestion_count = int(row[2] or 0)


def adopt(db: Session, suggestion_id: int, actor: str) -> RewriteOut:
    suggestion = get_suggestion(db, suggestion_id)
    if suggestion.status == SuggestionStatus.ADOPTED.value:
        return serializers.rewrite_item(suggestion)
    suggestion.status = SuggestionStatus.ADOPTED.value
    suggestion.reviewed_by = actor
    suggestion.reviewed_at = datetime.now(timezone.utc)
    # 采纳只改状态 + 刷新所属批次快照里的那条收益；重复采纳不会累计第二份。
    _attribute_to_latest_run(db, suggestion)
    _sync_item_snapshot(db, suggestion)
    _refresh_run_totals(db, suggestion.estimate_run_id)
    db.commit()
    db.refresh(suggestion)
    return serializers.rewrite_item(suggestion)


def export_rows(db: Session) -> list[dict]:
    rows, _ = list_suggestions(db, PageParams(page=1, page_size=200))
    return [row.model_dump() for row in rows]


def export_csv(db: Session) -> tuple[str, int]:
    rows = export_rows(db)
    return suggestions_csv(rows), len(rows)
