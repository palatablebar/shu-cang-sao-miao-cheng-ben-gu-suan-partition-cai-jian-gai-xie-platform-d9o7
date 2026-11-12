"""算法 1 的批量入口：逐条估算 + 批次聚合（扫描字节 / 费用 / 失效条数 / Top1）。

批次聚合还会带上算法 2 冻结的建议收益（``saving_bytes``），供预算页区分
「已确认 / 待确认节省」——收益只在本批次计一次，不随日志重导或元数据变化重算。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.analysis.sql_analyzer import QueryAnalysis, analyze_query, catalog_from_tables

GIB = 1024 ** 3


@dataclass
class EstimateRow:
    query_id: int
    analysis: QueryAnalysis
    rewrite_saving_bytes: int = 0
    rewrite_saving_cost_cny: float = 0.0
    suggestion_id: int | None = None


@dataclass
class EstimateSummary:
    sampled_count: int = 0
    query_count: int = 0
    parsed_count: int = 0
    blocked_count: int = 0
    prunable_count: int = 0
    total_scan_bytes: int = 0
    total_cost_cny: float = 0.0
    monthly_projected_cost_cny: float = 0.0
    saved_by_rewrites_bytes: int = 0
    saved_by_rewrites_cost_cny: float = 0.0
    suggestion_count: int = 0
    top_query_id: int | None = None
    top_scan_bytes: int = 0
    rows: list[EstimateRow] = field(default_factory=list)


def summarize_rows(
    rows: list[EstimateRow],
    cost_per_gib_cny: float,
    monthly_query_days: int = 30,
    query_volume_scale: float = 1.0,
) -> EstimateSummary:
    """Aggregate already-analysed rows into one batch summary."""
    summary = EstimateSummary(sampled_count=len(rows), rows=rows)
    for row in rows:
        analysis = row.analysis
        if analysis.parse_status != "parsed":
            continue
        summary.query_count += 1
        summary.parsed_count += 1
        if analysis.prunable:
            summary.prunable_count += 1
        else:
            summary.blocked_count += 1
        summary.total_scan_bytes += analysis.scan_bytes
        if analysis.scan_bytes > summary.top_scan_bytes:
            summary.top_scan_bytes = analysis.scan_bytes
            summary.top_query_id = row.query_id
        if row.rewrite_saving_bytes > 0:
            summary.saved_by_rewrites_bytes += row.rewrite_saving_bytes
            summary.saved_by_rewrites_cost_cny += row.rewrite_saving_cost_cny
            if row.suggestion_id is not None:
                summary.suggestion_count += 1

    summary.total_cost_cny = round(summary.total_scan_bytes / GIB * float(cost_per_gib_cny), 6)
    summary.monthly_projected_cost_cny = round(
        summary.total_cost_cny * monthly_query_days * query_volume_scale, 2
    )
    summary.saved_by_rewrites_cost_cny = round(summary.saved_by_rewrites_cost_cny, 6)
    return summary


def estimate_queries(
    items: list[tuple[int, str]],
    catalog,
    cost_per_gib_cny: float,
    monthly_query_days: int = 30,
    query_volume_scale: float = 1.0,
) -> EstimateSummary:
    """Estimate every (query_id, sql) pair against the metadata catalog."""
    rows = [EstimateRow(query_id=query_id, analysis=analyze_query(sql, catalog)) for query_id, sql in items]
    return summarize_rows(rows, cost_per_gib_cny, monthly_query_days, query_volume_scale)


def rows_from_tables(tables) -> dict:
    return catalog_from_tables(tables)
