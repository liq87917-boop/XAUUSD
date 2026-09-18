"""Phase 1 ORM 模型汇总（元数据注册唯一入口）。

导入本包会完成两件事（顺序不可调换）：
1. 导入全部 ORM 模型类 → 注册到 ``Base.metadata``（Alembic 与 ``create_all`` 依赖）；
2. 为研究事实表开启"不可覆盖"守卫（raw_items / raw_media / processed_items）。

因此：Alembic 的 ``env.py``、测试的 ``conftest.py``、以及任何需要建表/迁移的脚本，
都应 ``import database.models``（而不是逐个导入子模块），确保元数据完整。
"""

from database.base import Base
from database.models.author_lab import (
    AuthorOpinion,
    AuthorSkillSnapshot,
    AuthorWeightSnapshot,
    PropagationEdge,
)
from database.models.collector import CollectorRun
from database.models.enums import (
    TIMEFRAME_SECONDS,
    WEIGHT_CONTEXT_ANY,
    AssetClass,
    AuthorStatus,
    CollectorRunStatus,
    FeatureSetKind,
    InformationType,
    JobStatus,
    MediaType,
    OpinionHorizon,
    OpinionStance,
    ProcessStatus,
    PropagationRelation,
    RawItemType,
    Regime,
    SourceType,
    Timeframe,
)
from database.models.event import MacroEvent, NewsEvent
from database.models.feature import FeatureSet, FeatureSnapshot, FeatureValue, MarketRegime
from database.models.market import Instrument, MarketBar
from database.models.processing import AuthorPost, ProcessedItem
from database.models.raw import RawItem, RawMedia
from database.models.source import Author, AuthorAccount, Source
from database.models.system import AuditLog, DataVersion, JobRun
from database.protection import enable_immutability_protection

#: Phase 1 第一批实际建表清单（04 §42），顺序即建表依赖顺序
PHASE1_TABLES: tuple[str, ...] = (
    "sources",
    "authors",
    "author_accounts",
    "raw_items",
    "raw_media",
    "collector_runs",
    "processed_items",
    "author_posts",
    "instruments",
    "market_bars",
    "news_events",
    "macro_events",
    "job_runs",
    "data_versions",
    "audit_logs",
)

#: Phase 2（Author Lab）实际建表清单（04 §42「Phase 2 再建」+ 07 Phase 2 Prompt，
#: migration 0005），依赖 Phase 1 的 authors / author_posts / raw_items / instruments
PHASE2_TABLES: tuple[str, ...] = (
    "author_opinions",
    "propagation_edges",
    "author_skill_snapshots",
    "author_weight_snapshots",
)

PHASE3_TABLES: tuple[str, ...] = (
    "feature_sets",
    "feature_snapshots",
    "feature_values",
    "market_regimes",
)

#: 当前阶段（Phase 1 + Phase 2）应有的全部表；测试以此断言"不多不少"
ALL_TABLES: tuple[str, ...] = PHASE1_TABLES + PHASE2_TABLES + PHASE3_TABLES

#: 已注册"不可覆盖"守卫的表（原始 / 加工事实数据）
PROTECTED_TABLES: tuple[str, ...] = tuple(enable_immutability_protection(Base))

__all__ = [
    "ALL_TABLES",
    "PHASE1_TABLES",
    "PHASE2_TABLES",
    "PHASE3_TABLES",
    "PROTECTED_TABLES",
    "TIMEFRAME_SECONDS",
    "WEIGHT_CONTEXT_ANY",
    "AssetClass",
    "AuditLog",
    "Author",
    "AuthorAccount",
    "AuthorOpinion",
    "AuthorPost",
    "AuthorSkillSnapshot",
    "AuthorStatus",
    "AuthorWeightSnapshot",
    "Base",
    "CollectorRun",
    "CollectorRunStatus",
    "DataVersion",
    "FeatureSet",
    "FeatureSetKind",
    "FeatureSnapshot",
    "FeatureValue",
    "InformationType",
    "Instrument",
    "JobRun",
    "JobStatus",
    "MacroEvent",
    "MarketBar",
    "MarketRegime",
    "MediaType",
    "NewsEvent",
    "OpinionHorizon",
    "OpinionStance",
    "ProcessStatus",
    "ProcessedItem",
    "PropagationEdge",
    "PropagationRelation",
    "RawItem",
    "RawItemType",
    "RawMedia",
    "Regime",
    "Source",
    "SourceType",
    "Timeframe",
]
