"""EstimateRunItem — 批次内逐条查询的冻结快照（扫描量 + 建议收益）。

批次一旦完成，本表就是该批次的历史口径：
    * 重新导入日志、修改分区/列元数据都不会改写这些行；
    * 重试失败批次时先清空本批次的行，再在一个事务里重建，不会留下半批数据；
    * 唯一约束 (estimate_run_id, query_log_id) 保证同一批次同一条查询只计一次。
"""
from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class EstimateRunItem(Base, TimestampMixin):
    __tablename__ = "estimate_run_item"
    __table_args__ = (
        UniqueConstraint("estimate_run_id", "query_log_id", name="uq_estimate_run_item_run_query"),
        UniqueConstraint("estimate_run_id", "suggestion_id", name="uq_estimate_run_item_run_suggestion"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    estimate_run_id: Mapped[int] = mapped_column(
        ForeignKey("estimate_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    query_log_id: Mapped[int] = mapped_column(ForeignKey("query_log.id"), nullable=False, index=True)

    # --- 查询快照（算法 1 在批次执行时冻结）---
    external_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    target_table: Mapped[str | None] = mapped_column(String(96), nullable=True)
    parse_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unparsed")
    prunable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    hit_partition_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_partition_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scan_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    scan_cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- 建议收益快照（算法 2 在批次执行时冻结；状态仍以实时采纳结果为准）---
    suggestion_id: Mapped[int | None] = mapped_column(
        ForeignKey("rewrite_suggestion.id", ondelete="SET NULL"), nullable=True, index=True
    )
    suggestion_saving_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    suggestion_saving_cost_cny: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
