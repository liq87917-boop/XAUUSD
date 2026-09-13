"""Phase 2（Author Lab）表结构集成测试（migration 0005）。

覆盖（团队要求"严格的时间约束"，因此逐条落到数据库层验证）：

1. 4 张表存在且与 ORM 元数据一致（漂移测试另有专项）；
2. **时间因果**：``created_at >= effective_at / detected_at / as_of``
   —— 任何写入路径都不得自称"在未来才可用"（防未来数据泄漏的第一道门禁）；
3. **数值合法性**：confidence / similarity / weight / 技能分 0~1、entry 区间有序、
   价格 > 0、sample_size >= 0（NULL 表示"未计算"，禁止用 0 冒充）；
4. **枚举 CHECK**：stance / horizon / information_type / relation_type；
   其中 horizon 刻意不含 1m / 5m（只允许 Phase 2 的五个评价窗口）；
5. **幂等自然键**：传播边 / 技能快照 / 权重快照（含 'ANY' 哨兵维度）；
6. **不可覆盖**：UPDATE 与 DELETE 均被 ORM 守卫拒绝（append-only）；
7. **外键强制**：孤儿 author_id / author_post_id / raw_items 节点被拒绝。

时间取值：统一使用**过去的**固定时刻（2024-01-02 UTC），保证
``created_at``（写入时刻）永远晚于 ``effective_at`` 等业务时间字段，与运行时刻无关。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import database.models as models
from database.models import (
    AuthorOpinion,
    AuthorPost,
    AuthorSkillSnapshot,
    AuthorWeightSnapshot,
    Instrument,
    PropagationEdge,
)
from database.models.enums import (
    WEIGHT_CONTEXT_ANY,
    InformationType,
    OpinionHorizon,
    OpinionStance,
    PropagationRelation,
)
from database.seeds import seed_instruments
from src.common.exceptions import ImmutableRecordError

pytestmark = pytest.mark.integration

#: 业务时间（过去）与写入时间（now）必须严格分离，否则时间因果断言无意义
COLLECTED_AT = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
EARLIER = COLLECTED_AT - timedelta(hours=1)
LATER = COLLECTED_AT + timedelta(hours=1)
PARSER_VERSION = "mock-regex-v1"


# ---------------------------------------------------------------------------
# 夹具与构造器
# ---------------------------------------------------------------------------
@pytest.fixture()
def xauusd(session: Session) -> Instrument:
    """XAUUSD 标的（复用生产种子，保证与真实配置一致）。"""
    seed_instruments(session)
    session.flush()
    instrument = session.scalar(sa.select(Instrument).where(Instrument.symbol == "XAUUSD"))
    assert instrument is not None
    return instrument


@pytest.fixture()
def author_post(session: Session, make_author_account, make_raw_item) -> AuthorPost:
    """一条已归属的作者帖子（时间字段全部对齐 raw_items，满足 Phase 1 的 CHECK）。"""
    account = make_author_account()
    raw = make_raw_item(
        collected_at=COLLECTED_AT, published_at=COLLECTED_AT - timedelta(minutes=5)
    )
    post = AuthorPost(
        author_id=account.author_id,
        author_account_id=account.id,
        raw_item_id=raw.id,
        published_at=raw.published_at,
        collected_at=raw.collected_at,
        effective_at=raw.effective_at,
        text_content="XAUUSD 短线偏多，目标 2450，止损 2380",
        has_media=False,
    )
    session.add(post)
    session.flush()
    return post


def _opinion(
    author_post: AuthorPost, instrument: Instrument | None, **overrides: Any
) -> AuthorOpinion:
    """构造一条合法观点（默认值全部满足数据库约束；``instrument=None`` 表示不指标的）。"""
    defaults: dict[str, Any] = {
        "author_id": author_post.author_id,
        "author_post_id": author_post.id,
        "stance": OpinionStance.LONG,
        "instrument_id": instrument.id if instrument is not None else None,
        "horizon": OpinionHorizon.H1,
        "confidence": Decimal("0.7"),
        "entry_low": Decimal("2400"),
        "entry_high": Decimal("2410"),
        "stop_loss": Decimal("2380"),
        "take_profit": Decimal("2450"),
        "information_type": InformationType.TECHNICAL,
        "rationale": "短线偏多（原文：目标 2450）",
        "parser_version": PARSER_VERSION,
        "effective_at": author_post.effective_at,
    }
    defaults.update(overrides)
    return AuthorOpinion(**defaults)


def _flush_expect_error(session: Session, *instances: Any) -> None:
    """写入并断言数据库拒绝（IntegrityError），随后回滚保持 Session 可用。"""
    for instance in instances:
        session.add(instance)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ---------------------------------------------------------------------------
# 1) 表与字段
# ---------------------------------------------------------------------------
def test_phase2_tables_exist(engine: sa.Engine, migrated_engine: sa.Engine) -> None:
    for target in (engine, migrated_engine):
        tables = set(inspect(target).get_table_names())
        assert set(models.PHASE2_TABLES) <= tables


def test_opinion_persists_documented_fields(session: Session, author_post, xauusd) -> None:
    """正常路径：字段与类型按 04 §10 落库（含 Decimal 精度与枚举值）。"""
    opinion = _opinion(author_post, xauusd)
    session.add(opinion)
    session.flush()

    stored = session.get(AuthorOpinion, opinion.id)
    assert stored is not None
    assert stored.stance is OpinionStance.LONG
    assert stored.horizon is OpinionHorizon.H1
    assert stored.information_type is InformationType.TECHNICAL
    assert stored.parser_version == PARSER_VERSION
    assert stored.confidence == Decimal("0.7")
    assert stored.entry_low == Decimal("2400")
    assert stored.effective_at == author_post.effective_at
    assert stored.created_at >= stored.effective_at  # 时间因果 CHECK 的正常侧


def test_opinion_allows_unknown_stance_without_instrument(session: Session, author_post) -> None:
    """看不到明确方向/标的时，必须能显式写 UNKNOWN + 空标的（禁止猜测）。"""
    opinion = _opinion(
        author_post,
        instrument=None,
        stance=OpinionStance.UNKNOWN,
        horizon=None,
        confidence=None,
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        take_profit=None,
        information_type=None,
    )
    session.add(opinion)
    session.flush()
    assert opinion.instrument_id is None
    assert opinion.stance is OpinionStance.UNKNOWN
    assert opinion.effective_at == author_post.effective_at


# ---------------------------------------------------------------------------
# 2) 时间因果（数据库层强制）
# ---------------------------------------------------------------------------
def test_opinion_created_at_cannot_precede_effective_at(
    session: Session, author_post, xauusd
) -> None:
    """观点不得"自称"在未来才可用：created_at < effective_at 必须被拒绝。"""
    _flush_expect_error(
        session,
        _opinion(author_post, xauusd, created_at=EARLIER, effective_at=LATER),
    )


def test_propagation_edge_created_at_cannot_precede_detected_at(
    session: Session, make_raw_item
) -> None:
    first = make_raw_item()
    second = make_raw_item()
    _flush_expect_error(
        session,
        PropagationEdge(
            from_item_id=first.id,
            to_item_id=second.id,
            relation_type=PropagationRelation.REPOST,
            model_version="mock-v1",
            detected_at=LATER,
            created_at=EARLIER,
        ),
    )


def test_skill_snapshot_created_at_cannot_precede_as_of(session: Session, author_post) -> None:
    _flush_expect_error(
        session,
        AuthorSkillSnapshot(
            author_id=author_post.author_id,
            as_of=LATER,
            created_at=EARLIER,
            direction_skill=Decimal("0.6"),
            sample_size=30,
        ),
    )


def test_weight_snapshot_created_at_cannot_precede_as_of(session: Session, author_post) -> None:
    _flush_expect_error(
        session,
        AuthorWeightSnapshot(
            author_id=author_post.author_id,
            as_of=LATER,
            created_at=EARLIER,
            weight=Decimal("0.5"),
        ),
    )


# ---------------------------------------------------------------------------
# 3) 数值合法性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("confidence", [Decimal("1.5"), Decimal("-0.1")])
def test_opinion_confidence_must_be_in_unit_range(
    session: Session, author_post, xauusd, confidence: Decimal
) -> None:
    _flush_expect_error(session, _opinion(author_post, xauusd, confidence=confidence))


def test_opinion_entry_range_must_be_ordered(session: Session, author_post, xauusd) -> None:
    _flush_expect_error(
        session,
        _opinion(author_post, xauusd, entry_low=Decimal("2420"), entry_high=Decimal("2400")),
    )


@pytest.mark.parametrize("field", ["entry_low", "entry_high", "stop_loss", "take_profit"])
def test_opinion_prices_must_be_positive(
    session: Session, author_post, xauusd, field: str
) -> None:
    _flush_expect_error(session, _opinion(author_post, xauusd, **{field: Decimal("-1")}))


def test_skill_snapshot_sample_size_must_be_non_negative(session: Session, author_post) -> None:
    _flush_expect_error(
        session,
        AuthorSkillSnapshot(author_id=author_post.author_id, as_of=COLLECTED_AT, sample_size=-1),
    )


@pytest.mark.parametrize("score", [Decimal("1.2"), Decimal("-0.5")])
def test_skill_snapshot_scores_must_be_in_unit_range(
    session: Session, author_post, score: Decimal
) -> None:
    _flush_expect_error(
        session,
        AuthorSkillSnapshot(
            author_id=author_post.author_id,
            as_of=COLLECTED_AT,
            direction_skill=score,
            sample_size=10,
        ),
    )


@pytest.mark.parametrize("weight", [Decimal("1.5"), Decimal("-0.2")])
def test_weight_snapshot_weight_must_be_in_unit_range(
    session: Session, author_post, weight: Decimal
) -> None:
    _flush_expect_error(
        session,
        AuthorWeightSnapshot(author_id=author_post.author_id, as_of=COLLECTED_AT, weight=weight),
    )


# ---------------------------------------------------------------------------
# 4) 枚举 CHECK（绕过 ORM 直接写库，验证数据库层兜底）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        ("stance", "MOON"),
        ("horizon", "5m"),  # 刻意不在 Phase 2 的五个评价窗口内（只允许 15m/30m/1h/4h/1d）
        ("information_type", "GOSSIP"),
    ],
)
def test_opinion_enum_check_rejects_unknown_value(
    session: Session, author_post, xauusd, column: str, bad_value: str
) -> None:
    opinion = _opinion(author_post, xauusd)
    session.add(opinion)
    session.flush()
    with pytest.raises(IntegrityError):
        session.execute(sa.text(f"UPDATE author_opinions SET {column} = '{bad_value}'"))  # noqa: S608
    session.rollback()


def test_propagation_edge_relation_check_rejects_unknown_value(
    session: Session, make_raw_item
) -> None:
    first = make_raw_item()
    second = make_raw_item()
    session.add(
        PropagationEdge(
            from_item_id=first.id,
            to_item_id=second.id,
            relation_type=PropagationRelation.QUOTE,
            model_version="mock-v1",
            detected_at=COLLECTED_AT,
        )
    )
    session.flush()
    with pytest.raises(IntegrityError):
        session.execute(sa.text("UPDATE propagation_edges SET relation_type = 'LIKE'"))
    session.rollback()


# ---------------------------------------------------------------------------
# 5) 外键强制
# ---------------------------------------------------------------------------
def test_opinion_author_foreign_key_is_enforced(session: Session, author_post, xauusd) -> None:
    """孤儿 author_id 必须被拒绝（SQLite 也打开了 PRAGMA foreign_keys）。"""
    _flush_expect_error(session, _opinion(author_post, xauusd, author_id=uuid.uuid4()))


def test_opinion_post_foreign_key_is_enforced(session: Session, author_post, xauusd) -> None:
    _flush_expect_error(session, _opinion(author_post, xauusd, author_post_id=uuid.uuid4()))


def test_propagation_edge_node_foreign_key_is_enforced(
    session: Session, make_raw_item
) -> None:
    first = make_raw_item()
    _flush_expect_error(
        session,
        PropagationEdge(
            from_item_id=first.id,
            to_item_id=uuid.uuid4(),  # 不存在的原始节点
            relation_type=PropagationRelation.SEMANTIC_SIMILAR,
            model_version="mock-v1",
            detected_at=COLLECTED_AT,
        ),
    )


# ---------------------------------------------------------------------------
# 6) append-only（不可覆盖 + 禁止物理删除）
# ---------------------------------------------------------------------------
def test_opinion_update_is_rejected(session: Session, author_post, xauusd) -> None:
    """★ 研究事实表 append-only：已落库观点不得被改写（修正 = 新 parser_version）。"""
    opinion = _opinion(author_post, xauusd)
    session.add(opinion)
    session.flush()
    opinion.stance = OpinionStance.SHORT
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_opinion_delete_is_rejected(session: Session, author_post, xauusd) -> None:
    opinion = _opinion(author_post, xauusd)
    session.add(opinion)
    session.flush()
    session.delete(opinion)
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_propagation_edge_is_append_only(session: Session, make_raw_item) -> None:
    first = make_raw_item()
    second = make_raw_item()
    edge = PropagationEdge(
        from_item_id=first.id,
        to_item_id=second.id,
        relation_type=PropagationRelation.REPOST,
        model_version="mock-v1",
        detected_at=COLLECTED_AT,
    )
    session.add(edge)
    session.flush()
    session.delete(edge)
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_skill_snapshot_is_append_only(session: Session, author_post) -> None:
    snapshot = AuthorSkillSnapshot(
        author_id=author_post.author_id,
        as_of=COLLECTED_AT,
        direction_skill=Decimal("0.62"),
        sample_size=42,
    )
    session.add(snapshot)
    session.flush()
    snapshot.direction_skill = Decimal("0.99")
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


def test_weight_snapshot_is_append_only(session: Session, author_post) -> None:
    snapshot = AuthorWeightSnapshot(
        author_id=author_post.author_id, as_of=COLLECTED_AT, weight=Decimal("0.35")
    )
    session.add(snapshot)
    session.flush()
    snapshot.weight = Decimal("0.95")
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()


# ---------------------------------------------------------------------------
# 7) 幂等自然键（重复写入必须被拒绝）
# ---------------------------------------------------------------------------
def test_propagation_edge_duplicate_is_rejected(session: Session, make_raw_item) -> None:
    first = make_raw_item()
    second = make_raw_item()

    def _edge() -> PropagationEdge:
        return PropagationEdge(
            from_item_id=first.id,
            to_item_id=second.id,
            relation_type=PropagationRelation.SEMANTIC_SIMILAR,
            model_version="mock-v1",
            detected_at=COLLECTED_AT,
            similarity=Decimal("0.93"),
        )

    session.add(_edge())
    session.flush()
    _flush_expect_error(session, _edge())


def test_propagation_edge_self_loop_is_rejected(session: Session, make_raw_item) -> None:
    item = make_raw_item()
    _flush_expect_error(
        session,
        PropagationEdge(
            from_item_id=item.id,
            to_item_id=item.id,
            relation_type=PropagationRelation.SAME_SOURCE,
            model_version="mock-v1",
            detected_at=COLLECTED_AT,
        ),
    )


def test_skill_snapshot_is_unique_per_author_and_as_of(session: Session, author_post) -> None:
    def _snapshot() -> AuthorSkillSnapshot:
        return AuthorSkillSnapshot(
            author_id=author_post.author_id,
            as_of=COLLECTED_AT,
            direction_skill=Decimal("0.6"),
            sample_size=30,
        )

    session.add(_snapshot())
    session.flush()
    _flush_expect_error(session, _snapshot())


def test_weight_snapshot_is_unique_per_context(session: Session, author_post) -> None:
    def _snapshot() -> AuthorWeightSnapshot:
        return AuthorWeightSnapshot(
            author_id=author_post.author_id, as_of=COLLECTED_AT, weight=Decimal("0.5")
        )

    session.add(_snapshot())
    session.flush()
    _flush_expect_error(session, _snapshot())


def test_weight_snapshot_defaults_to_any_context(session: Session, author_post) -> None:
    """未指定维度时写入哨兵 'ANY'（NULL 会让唯一约束失去幂等意义）。"""
    snapshot = AuthorWeightSnapshot(
        author_id=author_post.author_id, as_of=COLLECTED_AT, weight=Decimal("0.5")
    )
    session.add(snapshot)
    session.flush()

    stored = session.get(AuthorWeightSnapshot, snapshot.id)
    assert stored is not None
    assert stored.regime_type == WEIGHT_CONTEXT_ANY
    assert stored.horizon == WEIGHT_CONTEXT_ANY
    assert stored.information_type == WEIGHT_CONTEXT_ANY


# ---------------------------------------------------------------------------
# 8) 迁移可回滚（0005 downgrade 只移除 Phase 2 的四张表）
# ---------------------------------------------------------------------------
def test_downgrade_to_0004_removes_only_phase2_tables(
    tmp_path, alembic_config_factory
) -> None:
    from alembic import command

    from database.session import build_engine

    database_url = f"sqlite+pysqlite:///{(tmp_path / 'phase2_downgrade.db').as_posix()}"
    config = alembic_config_factory(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "0004_collector_run_warnings")

    engine = build_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert set(models.PHASE2_TABLES).isdisjoint(tables)
        assert set(models.PHASE1_TABLES) <= tables
    finally:
        engine.dispose()
