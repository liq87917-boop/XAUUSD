"""Phase 1 表结构集成测试（04 §42：第一批 15 张表）。

覆盖：
- 表清单严格等于文档规定的 15 张（不提前建 Phase 2/3 的表 —— 阶段锁定）
- 所有时间列 timezone-aware（06_Cline开发规则 第 7 条）
- 幂等唯一键（source / author_account / raw_item / job_run）
- 外键强制（SQLite 也打开 PRAGMA foreign_keys）
- 枚举 CHECK 约束在数据库层兜底
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

import database.models as models
from database.models import AuthorAccount, JobRun, RawItem, RawItemType, Source, SourceType
from src.common.hashing import content_hash
from src.common.time import utc_now

pytestmark = pytest.mark.integration


def test_tables_match_documented_set(engine: sa.Engine) -> None:
    """表清单严格等于文档规定的集合（Phase 1 十五张 + Phase 2 四张，不多不少）。"""
    assert set(inspect(engine).get_table_names()) == set(models.ALL_TABLES)


def test_later_phase_tables_are_not_created(engine: sa.Engine) -> None:
    """阶段锁定（05_分阶段开发路线图）：后续阶段（Phase 3+）的表不得提前出现。

    Phase 2（Author Lab）的四张表已由 migration 0005 正式建立，因此从本清单移除；
    Phase 3 及以后的表（特征 / Regime / Alpha / 策略 / 回测 / 风险 / 进化）继续禁止。
    """
    forbidden = {
        "feature_sets",
        "feature_snapshots",
        "market_regimes",
        "alpha_models",
        "alpha_signals",
        "predictions",
        "ensemble_runs",
        "strategies",
        "strategy_versions",
        "backtest_runs",
        "risk_evaluations",
        "paper_orders",
        "evolution_rounds",
    }
    assert forbidden.isdisjoint(set(inspect(engine).get_table_names()))


@pytest.mark.parametrize("table", models.ALL_TABLES)
def test_timestamp_columns_are_timezone_aware(table: str) -> None:
    """基于 ORM 元数据断言：不得存在 naive 时间列（SQLite 反射会丢失 tz 信息）。"""
    offenders = [
        column.name
        for column in models.Base.metadata.tables[table].columns
        if isinstance(column.type, sa.DateTime) and not column.type.timezone
    ]
    assert offenders == []


@pytest.mark.parametrize("table", models.ALL_TABLES)
def test_primary_key_is_uuid_v7_column(table: str) -> None:
    column = models.Base.metadata.tables[table].c.id
    assert column.primary_key
    assert isinstance(column.type, sa.Uuid)
    assert column.default is not None  # 由 uuid7 生成


def test_phase1_has_exactly_fifteen_tables() -> None:
    assert len(models.PHASE1_TABLES) == 15


def test_phase2_has_exactly_four_tables() -> None:
    """07 Phase 2 Prompt 明确要求建立 4 张表（migration 0005）。"""
    assert len(models.PHASE2_TABLES) == 4
    assert set(models.PHASE2_TABLES) <= set(models.ALL_TABLES)


def test_source_name_is_unique(session: sa.orm.Session, make_source) -> None:
    source = make_source()
    session.add(Source(name=source.name, source_type=SourceType.NEWS))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_author_account_is_unique_per_platform(session, make_author_account) -> None:
    account = make_author_account()
    session.add(
        AuthorAccount(
            author_id=account.author_id,
            source_id=account.source_id,
            external_account_id=account.external_account_id,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_raw_item_duplicate_source_record_is_rejected(session, make_raw_item) -> None:
    """同一来源的同一外部记录不得产生第二条"活动"记录（幂等）。"""
    item = make_raw_item()
    session.add(
        RawItem(
            source_id=item.source_id,
            source_record_id=item.source_record_id,
            item_type=RawItemType.POST,
            raw_json={"duplicate": True},
            content_hash=content_hash("duplicate"),
            published_at=item.published_at,
            collected_at=item.collected_at,
            effective_at=item.effective_at,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_foreign_key_violation_is_rejected(session) -> None:
    now = utc_now()
    session.add(
        RawItem(
            source_id=uuid.uuid4(),
            source_record_id="orphan-record",
            item_type=RawItemType.POST,
            raw_json={},
            content_hash=content_hash("orphan"),
            published_at=None,
            collected_at=now,
            effective_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_job_runs_idempotency_key_is_unique(session) -> None:
    now = utc_now()
    first = JobRun(job_type="collect_weibo", idempotency_key="2026-09-12T08:00", scheduled_at=now)
    duplicate = JobRun(
        job_type="collect_weibo", idempotency_key="2026-09-12T08:00", scheduled_at=now
    )
    session.add(first)
    session.flush()
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_enum_check_constraint_rejects_unknown_value(session, make_source) -> None:
    """绕过 ORM 类型直接写库时，数据库 CHECK 仍必须拦截非法枚举值。"""
    make_source()
    session.flush()
    with pytest.raises(IntegrityError):
        session.execute(sa.text("UPDATE sources SET source_type = 'TIKTOK'"))
    session.rollback()


def test_enum_check_constraint_accepts_documented_value(session, make_source) -> None:
    make_source()
    session.flush()
    session.execute(sa.text("UPDATE sources SET source_type = 'NEWS'"))
    session.flush()
