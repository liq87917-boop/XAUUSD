"""Phase 1 核心表结构（Gold Intelligence Database）

Revision ID: 0001_phase1_core_tables
Revises:
Create Date: 2026-09-12

对应文档：
- 04_数据表结构及字段定义 第 42 节：Phase 1 第一批实际建表（15 张）
- 03_数据库完整设计：UUID 主键、TIMESTAMPTZ、JSONB、NUMERIC 精度、不可覆盖原则

实现说明（重要，阅读后再修改本文件）：
1. 本迁移**手写 DDL**，不使用在 SQLite 上 autogenerate 的产物：生产/研究环境唯一
   目标是 PostgreSQL，JSONB / UUID / TIMESTAMPTZ 必须真实落库。``database.types``
   提供同一份类型对象（PG 原生 + SQLite 测试变体），因此迁移既能在 PG 上建出
   原生 JSONB，也能在 SQLite 测试库上执行（本文件必须同时对两者可用）。
2. 枚举列统一为 ``VARCHAR(n) + CHECK``（不使用 native enum），且枚举取值在本文件中
   **冻结**：即使未来 Python 枚举类新增取值，本 revision 产生的历史结构也不会漂移
   （03_数据库完整设计 第 19 节：禁止修改已上线 migration）。
3. 未显式命名的 PK / FK / 单列唯一约束 / 索引由 SQLAlchemy 命名约定生成；
   Alembic 会自动继承 ``target_metadata.naming_convention``
   （alembic/operations/schemaobj.py），因此与 ORM 元数据完全一致，
   并由 tests/integration/test_migration_matches_metadata.py 严格比对。
4. 表内 CHECK 约束把时间因果与数值合法性下沉到数据库层，防止任何写入路径绕过
   （原始数据不可覆盖 / 无未来数据泄漏 / OHLC 合法）：
   - effective_at >= collected_at、effective_at >= published_at、event_at <= effective_at
   - market_bars：high/low 边界、price > 0、close_time > open_time
5. downgrade 为严格逆序 drop，可完整回滚（无不可回滚项）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from database.types import (
    JSONB,
    MACRO_VALUE,
    PRICE,
    PROBABILITY,
    PROBABILITY_ANY,
    TIMESTAMP,
    UUID,
    VOLUME,
)

# revision identifiers, used by Alembic.
revision: str = "0001_phase1_core_tables"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str, length: int) -> sa.Enum:
    """构造 VARCHAR(length) + CHECK 枚举类型（取值在本文件冻结）。"""
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
    )


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1) 信息源与作者
    # ------------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "source_type",
            _enum("WEIBO", "NEWS", "MARKET", "MACRO", name="source_type", length=50),
            nullable=False,
        ),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("timezone", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_json", JSONB, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "authors",
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("canonical_name", sa.String(length=200), nullable=True),
        sa.Column(
            "status",
            _enum(
                "ACTIVE",
                "EXPLORATION",
                "PROBATION",
                "DISABLED",
                name="author_status",
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("style_embedding_ref", sa.Text(), nullable=True),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_name"),
    )

    op.create_table(
        "author_accounts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.Column("author_id", UUID, nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("external_account_id", sa.String(length=200), nullable=False),
        sa.Column("account_name", sa.String(length=200), nullable=True),
        sa.Column("profile_url", sa.Text(), nullable=True),
        sa.Column("follower_count", sa.BigInteger(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_collected_at", TIMESTAMP, nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["authors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "external_account_id", name="uq_author_accounts_platform_account"
        ),
    )

    # ------------------------------------------------------------------
    # 2) 原始数据与采集（append-only，禁止覆盖）
    # ------------------------------------------------------------------
    op.create_table(
        "raw_items",
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("source_record_id", sa.String(length=300), nullable=False),
        sa.Column(
            "item_type",
            _enum("POST", "NEWS", "MACRO", "QUOTE", name="raw_item_type", length=50),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published_at", TIMESTAMP, nullable=True),
        sa.Column("collected_at", TIMESTAMP, nullable=False),
        sa.Column("effective_at", TIMESTAMP, nullable=False),
        sa.Column("superseded_by_id", UUID, nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["raw_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # 幂等键（03_数据库完整设计 第 7 节索引重点）
        sa.UniqueConstraint("source_id", "source_record_id", name="uq_raw_items_source_record"),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at <= effective_at",
            name="published_at_le_effective_at",
        ),
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
    )

    op.create_table(
        "raw_media",
        sa.Column("raw_item_id", UUID, nullable=False),
        sa.Column(
            "media_type",
            _enum("IMAGE", "VIDEO", "PDF", name="media_type", length=30),
            nullable=False,
        ),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(length=100), nullable=True),
        sa.Column("collected_at", TIMESTAMP, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["raw_item_id"], ["raw_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_item_id", "sha256", name="uq_raw_media_item_sha256"),
        sa.CheckConstraint("width IS NULL OR width > 0", name="width_positive"),
        sa.CheckConstraint("height IS NULL OR height > 0", name="height_positive"),
    )

    op.create_table(
        "collector_runs",
        sa.Column("collector_name", sa.String(length=100), nullable=False),
        sa.Column("source_id", UUID, nullable=True),
        sa.Column("started_at", TIMESTAMP, nullable=False),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column(
            "status",
            _enum(
                "PENDING",
                "RUNNING",
                "SUCCESS",
                "PARTIAL_FAILED",
                "FAILED",
                name="collector_run_status",
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("cursor_json", JSONB, nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
        sa.CheckConstraint("fetched_count >= 0", name="fetched_count_non_negative"),
        sa.CheckConstraint("inserted_count >= 0", name="inserted_count_non_negative"),
        sa.CheckConstraint("duplicate_count >= 0", name="duplicate_count_non_negative"),
        sa.CheckConstraint("failed_count >= 0", name="failed_count_non_negative"),
    )

    op.create_table(
        "processed_items",
        sa.Column("raw_item_id", UUID, nullable=False),
        sa.Column("processor_name", sa.String(length=100), nullable=False),
        sa.Column("processor_version", sa.String(length=50), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=20), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("structured_json", JSONB, nullable=True),
        sa.Column(
            "status",
            _enum(
                "PENDING",
                "PROCESSING",
                "SUCCESS",
                "FAILED",
                "RETRYING",
                "SKIPPED",
                name="process_status",
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("effective_at", TIMESTAMP, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["raw_item_id"], ["raw_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "raw_item_id",
            "processor_name",
            "processor_version",
            name="uq_processed_items_processor_version",
        ),
    )

    # ------------------------------------------------------------------
    # 3) 作者帖子与市场行情
    # ------------------------------------------------------------------
    op.create_table(
        "author_posts",
        sa.Column("author_id", UUID, nullable=False),
        sa.Column("author_account_id", UUID, nullable=False),
        sa.Column("raw_item_id", UUID, nullable=False),
        sa.Column("published_at", TIMESTAMP, nullable=True),
        sa.Column("collected_at", TIMESTAMP, nullable=False),
        sa.Column("effective_at", TIMESTAMP, nullable=False),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("has_media", sa.Boolean(), nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["authors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["author_account_id"], ["author_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["raw_item_id"], ["raw_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_item_id", name="uq_author_posts_raw_item"),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at <= effective_at",
            name="published_at_le_effective_at",
        ),
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
    )

    op.create_table(
        "instruments",
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column(
            "asset_class",
            _enum(
                "METAL",
                "FX",
                "INDEX",
                "BOND",
                "ENERGY",
                "COMMODITY",
                "CRYPTO",
                name="asset_class",
                length=50,
            ),
            nullable=False,
        ),
        sa.Column("quote_currency", sa.String(length=20), nullable=True),
        sa.Column("timezone", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )

    op.create_table(
        "market_bars",
        sa.Column("instrument_id", UUID, nullable=False),
        sa.Column(
            "timeframe",
            _enum("1m", "5m", "15m", "30m", "1h", "4h", "1d", name="timeframe", length=10),
            nullable=False,
        ),
        sa.Column("open_time", TIMESTAMP, nullable=False),
        sa.Column("close_time", TIMESTAMP, nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", VOLUME, nullable=True),
        sa.Column("source_id", UUID, nullable=True),
        sa.Column("collected_at", TIMESTAMP, nullable=False),
        sa.Column("effective_at", TIMESTAMP, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "timeframe",
            "open_time",
            name="uq_market_bars_instrument_timeframe_open",
        ),
        sa.CheckConstraint(
            "high >= open AND high >= close AND low <= open AND low <= close",
            name="ohlc_bounds_valid",
        ),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"
        ),
        sa.CheckConstraint("close_time > open_time", name="bar_time_order"),
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
        sa.CheckConstraint("volume IS NULL OR volume >= 0", name="volume_non_negative"),
    )

    # ------------------------------------------------------------------
    # 4) 新闻与宏观事件
    # ------------------------------------------------------------------
    op.create_table(
        "news_events",
        sa.Column("raw_item_id", UUID, nullable=False),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=True),
        sa.Column("importance", PROBABILITY, nullable=True),
        sa.Column("sentiment", PROBABILITY_ANY, nullable=True),
        sa.Column("event_at", TIMESTAMP, nullable=True),
        sa.Column("published_at", TIMESTAMP, nullable=False),
        sa.Column("effective_at", TIMESTAMP, nullable=False),
        sa.Column("parser_version", sa.String(length=50), nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["raw_item_id"], ["raw_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "raw_item_id", "parser_version", name="uq_news_events_raw_item_parser_version"
        ),
        sa.CheckConstraint(
            "importance IS NULL OR (importance >= 0 AND importance <= 1)",
            name="importance_probability_range",
        ),
        sa.CheckConstraint(
            "sentiment IS NULL OR (sentiment >= -1 AND sentiment <= 1)",
            name="sentiment_range",
        ),
        sa.CheckConstraint("published_at <= effective_at", name="published_at_le_effective_at"),
    )

    op.create_table(
        "macro_events",
        sa.Column("event_code", sa.String(length=100), nullable=False),
        sa.Column("country", sa.String(length=50), nullable=False),
        sa.Column("event_at", TIMESTAMP, nullable=False),
        sa.Column("actual_value", MACRO_VALUE, nullable=True),
        sa.Column("forecast_value", MACRO_VALUE, nullable=True),
        sa.Column("previous_value", MACRO_VALUE, nullable=True),
        sa.Column("unit", sa.String(length=30), nullable=True),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("collected_at", TIMESTAMP, nullable=False),
        sa.Column("effective_at", TIMESTAMP, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "event_code",
            "country",
            "event_at",
            name="uq_macro_events_source_code_country_event_at",
        ),
        sa.CheckConstraint("event_at <= effective_at", name="event_at_le_effective_at"),
        sa.CheckConstraint("collected_at <= effective_at", name="collected_at_le_effective_at"),
    )

    # ------------------------------------------------------------------
    # 5) 系统基础表（调度幂等 / 数据版本 / 审计）
    # ------------------------------------------------------------------
    op.create_table(
        "job_runs",
        sa.Column("job_type", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("scheduled_at", TIMESTAMP, nullable=False),
        sa.Column("started_at", TIMESTAMP, nullable=True),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column(
            "status",
            _enum(
                "PENDING",
                "RUNNING",
                "SUCCESS",
                "PARTIAL_FAILED",
                "RETRYING",
                "FAILED",
                "CANCELLED",
                name="job_status",
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("input_json", JSONB, nullable=True),
        sa.Column("output_json", JSONB, nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
        sa.CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
    )

    op.create_table(
        "data_versions",
        sa.Column("dataset_name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("start_at", TIMESTAMP, nullable=True),
        sa.Column("end_at", TIMESTAMP, nullable=True),
        sa.Column("data_hash", sa.String(length=64), nullable=True),
        sa.Column("source_scope_json", JSONB, nullable=True),
        sa.Column("filter_json", JSONB, nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("generated_at", TIMESTAMP, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_name", "version", name="uq_data_versions_dataset_version"),
        sa.CheckConstraint(
            "start_at IS NULL OR end_at IS NULL OR end_at >= start_at", name="period_order"
        ),
    )

    op.create_table(
        "audit_logs",
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=True),
        sa.Column("entity_id", UUID, nullable=True),
        sa.Column("before_json", JSONB, nullable=True),
        sa.Column("after_json", JSONB, nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # 6) 索引（名称与 ORM 元数据一一对应，由漂移测试强制校验）
    # ------------------------------------------------------------------
    op.create_index("ix_author_accounts_author_id", "author_accounts", ["author_id"])
    op.create_index("ix_author_accounts_source_id", "author_accounts", ["source_id"])
    op.create_index("ix_raw_items_published_at", "raw_items", ["published_at"])
    op.create_index("ix_raw_items_collected_at", "raw_items", ["collected_at"])
    op.create_index("ix_raw_items_content_hash", "raw_items", ["content_hash"])
    op.create_index("ix_raw_media_raw_item_id", "raw_media", ["raw_item_id"])
    op.create_index("ix_raw_media_sha256", "raw_media", ["sha256"])
    op.create_index(
        "ix_collector_runs_name_started_at", "collector_runs", ["collector_name", "started_at"]
    )
    op.create_index("ix_collector_runs_status", "collector_runs", ["status"])
    op.create_index("ix_processed_items_raw_item_id", "processed_items", ["raw_item_id"])
    op.create_index(
        "ix_processed_items_status_created_at", "processed_items", ["status", "created_at"]
    )
    op.create_index(
        "ix_author_posts_author_account_id", "author_posts", ["author_account_id"]
    )
    op.create_index(
        "ix_author_posts_author_published_at", "author_posts", ["author_id", "published_at"]
    )
    op.create_index(
        "ix_market_bars_instrument_timeframe_close_time",
        "market_bars",
        ["instrument_id", "timeframe", "close_time"],
    )
    op.create_index("ix_market_bars_open_time", "market_bars", ["open_time"])
    op.create_index("ix_news_events_raw_item_id", "news_events", ["raw_item_id"])
    op.create_index("ix_news_events_published_at", "news_events", ["published_at"])
    op.create_index("ix_news_events_event_type", "news_events", ["event_type"])
    op.create_index("ix_macro_events_source_id", "macro_events", ["source_id"])
    op.create_index("ix_macro_events_event_at", "macro_events", ["event_at"])
    op.create_index("ix_macro_events_code_country", "macro_events", ["event_code", "country"])
    op.create_index("ix_job_runs_status_scheduled_at", "job_runs", ["status", "scheduled_at"])
    op.create_index("ix_data_versions_generated_at", "data_versions", ["generated_at"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    """严格逆序回滚（本 revision 无不可回滚项）。"""
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_index("ix_data_versions_generated_at", table_name="data_versions")
    op.drop_index("ix_job_runs_status_scheduled_at", table_name="job_runs")
    op.drop_index("ix_macro_events_code_country", table_name="macro_events")
    op.drop_index("ix_macro_events_event_at", table_name="macro_events")
    op.drop_index("ix_macro_events_source_id", table_name="macro_events")
    op.drop_index("ix_news_events_event_type", table_name="news_events")
    op.drop_index("ix_news_events_published_at", table_name="news_events")
    op.drop_index("ix_news_events_raw_item_id", table_name="news_events")
    op.drop_index("ix_market_bars_open_time", table_name="market_bars")
    op.drop_index("ix_market_bars_instrument_timeframe_close_time", table_name="market_bars")
    op.drop_index("ix_author_posts_author_published_at", table_name="author_posts")
    op.drop_index("ix_author_posts_author_account_id", table_name="author_posts")
    op.drop_index("ix_processed_items_status_created_at", table_name="processed_items")
    op.drop_index("ix_processed_items_raw_item_id", table_name="processed_items")
    op.drop_index("ix_collector_runs_status", table_name="collector_runs")
    op.drop_index("ix_collector_runs_name_started_at", table_name="collector_runs")
    op.drop_index("ix_raw_media_sha256", table_name="raw_media")
    op.drop_index("ix_raw_media_raw_item_id", table_name="raw_media")
    op.drop_index("ix_raw_items_content_hash", table_name="raw_items")
    op.drop_index("ix_raw_items_collected_at", table_name="raw_items")
    op.drop_index("ix_raw_items_published_at", table_name="raw_items")
    op.drop_index("ix_author_accounts_source_id", table_name="author_accounts")
    op.drop_index("ix_author_accounts_author_id", table_name="author_accounts")

    op.drop_table("audit_logs")
    op.drop_table("data_versions")
    op.drop_table("job_runs")
    op.drop_table("macro_events")
    op.drop_table("news_events")
    op.drop_table("market_bars")
    op.drop_table("instruments")
    op.drop_table("author_posts")
    op.drop_table("processed_items")
    op.drop_table("collector_runs")
    op.drop_table("raw_media")
    op.drop_table("raw_items")
    op.drop_table("author_accounts")
    op.drop_table("authors")
    op.drop_table("sources")







