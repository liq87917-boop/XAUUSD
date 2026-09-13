"""Phase 1 枚举取值（严格对齐 04_数据表结构及字段定义 与 02_系统详细设计）。

约定：
- 统一使用 ``StrEnum``，可直接 JSON 序列化，也可与数据库 VARCHAR 值直接比较。
- 数据库侧为 ``VARCHAR(n) + CHECK``（见 ``database.types.enum_type``），
  不使用 PostgreSQL native enum，避免历史 migration 的 ALTER TYPE 维护成本。
- 枚举 ``value`` 即为落库值；成员名可与值不同（如 ``M1 = "1m"``），
  因此所有枚举列必须通过 ``values_callable`` 存储 value。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "TIMEFRAME_SECONDS",
    "AssetClass",
    "AuthorStatus",
    "CollectorRunStatus",
    "InformationType",
    "JobStatus",
    "MediaType",
    "OpinionHorizon",
    "OpinionStance",
    "ProcessStatus",
    "PropagationRelation",
    "RawItemType",
    "SourceType",
    "Timeframe",
    "WEIGHT_CONTEXT_ANY",
]


class SourceType(StrEnum):
    """来源类型（04 §2 sources.source_type）。"""

    WEIBO = "WEIBO"
    NEWS = "NEWS"
    MARKET = "MARKET"
    MACRO = "MACRO"


class AuthorStatus(StrEnum):
    """作者状态（02 §5.6）。低权重作者进入 PROBATION，而不是永久删除。"""

    ACTIVE = "ACTIVE"
    EXPLORATION = "EXPLORATION"
    PROBATION = "PROBATION"
    DISABLED = "DISABLED"


class RawItemType(StrEnum):
    """原始数据类型（04 §5 raw_items.item_type）。"""

    POST = "POST"
    NEWS = "NEWS"
    MACRO = "MACRO"
    QUOTE = "QUOTE"


class MediaType(StrEnum):
    """媒体类型（04 §6 raw_media.media_type）。"""

    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    PDF = "PDF"


class CollectorRunStatus(StrEnum):
    """采集运行状态（04 §7 collector_runs.status）。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"


class ProcessStatus(StrEnum):
    """Processor 状态机（02 §5.2）。"""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    SKIPPED = "SKIPPED"


class JobStatus(StrEnum):
    """调度任务状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssetClass(StrEnum):
    """标的资产类别（04 §12 instruments.asset_class）。"""

    METAL = "METAL"
    FX = "FX"
    INDEX = "INDEX"
    BOND = "BOND"
    ENERGY = "ENERGY"
    COMMODITY = "COMMODITY"
    CRYPTO = "CRYPTO"


class Timeframe(StrEnum):
    """K 线周期（04 §13 market_bars.timeframe，落库值 04 文档同形）。"""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


#: 周期长度（秒），用于数据质量检查（K 线缺口、close_time - open_time 校验）
TIMEFRAME_SECONDS: dict[str, int] = {
    Timeframe.M1.value: 60,
    Timeframe.M5.value: 300,
    Timeframe.M15.value: 900,
    Timeframe.M30.value: 1800,
    Timeframe.H1.value: 3600,
    Timeframe.H4.value: 14400,
    Timeframe.D1.value: 86400,
}


# ---------------------------------------------------------------------------
# Phase 2（Author Lab）：观点 / 传播 / 技能 / 权重
# ---------------------------------------------------------------------------
class OpinionStance(StrEnum):
    """观点方向（04 §10 author_opinions.stance）。

    ``UNKNOWN`` 是**合法落库值**：当文本无法判定方向时必须显式写 UNKNOWN，
    不得猜测（与新闻时区不明确时的处理口径一致）。
    """

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class OpinionHorizon(StrEnum):
    """观点周期（04 §10 author_opinions.horizon，05 Phase 2 评价窗口）。

    刻意**不含** ``1m`` / ``5m``：Phase 2 的前瞻评价窗口只有
    15m / 30m / 1h / 4h / 1d（05_分阶段开发路线图 Phase 2），
    ``Timeframe`` 中更细的周期只用于行情数据本身，不用于作者技能评价。
    """

    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class InformationType(StrEnum):
    """信息类型（04 §10 author_opinions.information_type，取值为可扩展集合）。

    04 文档写的是 ``MACRO/TECHNICAL/NEWS/...``：省略号表示会持续扩充。
    这里先把文档明确列出的三类固定下来，并补 ``SENTIMENT`` / ``POSITIONING``
    两个黄金市场常见类型与兜底 ``OTHER``；新增取值必须通过新的 Alembic 迁移
    更新 CHECK（禁止手改已发布迁移）。
    """

    MACRO = "MACRO"
    TECHNICAL = "TECHNICAL"
    NEWS = "NEWS"
    SENTIMENT = "SENTIMENT"
    POSITIONING = "POSITIONING"
    OTHER = "OTHER"


class PropagationRelation(StrEnum):
    """传播关系类型（04 §11 propagation_edges.relation_type）。"""

    REPOST = "REPOST"
    QUOTE = "QUOTE"
    SEMANTIC_SIMILAR = "SEMANTIC_SIMILAR"
    SAME_SOURCE = "SAME_SOURCE"


#: author_weight_snapshots 的维度缺省值（"不区分该维度"）。
#: 为什么用哨兵值而不是 NULL：``(author_id, regime_type, horizon, information_type, as_of)``
#: 是权重快照的幂等自然键，而 SQL 唯一约束对 NULL 不去重（PostgreSQL 视 NULL 互不相等），
#: 用 NULL 会让"同一作者同一时点同一上下文"重复落库。哨兵 'ANY' 让幂等约束真正生效。
WEIGHT_CONTEXT_ANY: str = "ANY"
