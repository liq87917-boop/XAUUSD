"""Phase 3.3 Author Alpha 组装层（opinion_labels → SkillSample → author_skill_snapshots）。

职责边界（.clinerules 预测/策略/风险解耦红线）：
- 本层只做「组装 + 落库」，**不做技能计算**——计算在
  :mod:`src.alpha.author_alpha` 的纯函数里完成；
- 防泄漏：``as_of`` 由调用方传入，:func:`compute_author_skill` 内部按
  ``effective_at <= as_of`` 过滤，本层不额外猜测；
- 幂等：``author_skill_snapshots`` 唯一键 ``(author_id, as_of)``，写入前先查重，
  已存在则跳过（该表注册了 append-only 守卫，禁止 UPDATE，重复即跳过）。

``OpinionLabel`` 不携带 ``author_id`` / ``confidence`` / ``information_type``，
本层回查 ``author_opinions`` 补齐后再组装 :class:`SkillSample`。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import AuthorOpinion, AuthorSkillSnapshot
from src.alpha.author_alpha import AuthorSkillResult, SkillSample, compute_author_skill
from src.common.time import parse_iso8601
from src.processors.opinion_labels import OpinionLabel

__all__ = [
    "AuthorSample",
    "build_skill_metadata",
    "compute_all_author_skills",
    "label_to_sample",
    "assemble_skill_samples",
    "persist_skill_snapshots",
]

#: 写入 ``metadata_json`` 的键名（报告分列与追溯依赖这些键，表结构无对应列）
META_DIRECTION_RAW: Final[str] = "direction_skill_raw"
META_DIRECTION_HITS: Final[str] = "direction_hits"
META_TIMING_RAW: Final[str] = "timing_skill_raw"
META_READY: Final[str] = "ready"


@dataclass(frozen=True, slots=True)
class AuthorSample:
    """作者归属 + 单条技能样本（组装层把 ``OpinionLabel`` 补齐归属后得到）。"""

    author_id: str
    sample: SkillSample


def label_to_sample(
    label: OpinionLabel,
    *,
    author_id: str,
    confidence: float | None,
    information_type: str | None,
) -> SkillSample | None:
    """把单条 :class:`OpinionLabel` 转为 :class:`SkillSample`。

    只有 ``status == "LABELED"`` 且 ``stance_direction`` / ``direction_hit`` 齐备的
    标签才可进入技能评估；其余状态（NO_ENTRY_BAR / ENTRY_LAG_EXCEEDED /
    GAP_IN_HORIZON / ...）没有收益信息，返回 ``None``。
    """
    if label.status != "LABELED":
        return None
    if label.stance_direction is None or label.direction_hit is None:
        return None
    effective_at = parse_iso8601(label.effective_at, field_name="label.effective_at")
    return SkillSample(
        opinion_id=label.opinion_id,
        effective_at=effective_at,
        stance_direction=label.stance_direction,
        horizon=label.horizon,
        information_type=information_type,
        confidence=confidence,
        direction_hit=label.direction_hit,
        log_return=label.log_return,
        entry_lag_seconds=label.entry_lag_seconds,
    )


def assemble_skill_samples(
    session: Session, labels: Sequence[OpinionLabel]
) -> list[AuthorSample]:
    """从标签结果组装作者技能样本，补齐归属 / 置信度 / 信息类型。

    无法回查归属的观点直接跳过（不猜 author_id）。回查用 ``IN`` 一次取齐，
    避免逐条 N+1 查询。
    """
    if not labels:
        return []
    opinion_uuids = {uuid.UUID(label.opinion_id) for label in labels}
    opinions = {
        str(item.id): item
        for item in session.scalars(
            sa.select(AuthorOpinion).where(AuthorOpinion.id.in_(opinion_uuids))
        ).all()
    }
    result: list[AuthorSample] = []
    for label in labels:
        opinion = opinions.get(label.opinion_id)
        if opinion is None:
            continue
        sample = label_to_sample(
            label,
            author_id=str(opinion.author_id),
            confidence=_to_float(opinion.confidence),
            information_type=_enum_value(opinion.information_type),
        )
        if sample is not None:
            result.append(AuthorSample(author_id=str(opinion.author_id), sample=sample))
    return result


def compute_all_author_skills(
    samples: Sequence[AuthorSample],
    *,
    as_of: datetime,
) -> list[AuthorSkillResult]:
    """按作者分组计算技能快照（不落库），按 ``author_id`` 排序返回。

    ``as_of`` 必须 timezone-aware（由 :func:`compute_author_skill` 内部校验）。
    """
    by_author: dict[str, list[SkillSample]] = {}
    for item in samples:
        by_author.setdefault(item.author_id, []).append(item.sample)
    return [
        compute_author_skill(group, author_id=author_id, as_of=as_of)
        for author_id, group in sorted(by_author.items())
    ]


def build_skill_metadata(result: AuthorSkillResult) -> dict[str, Any]:
    """把技能结果里「表结构放不下」的原始值收进 ``metadata_json``。

    ``author_skill_snapshots`` 只存收缩后的 ``direction_skill`` / ``timing_skill``，
    原始命中率 / 原始有向收益均值（报告必须分列的 raw 列）只能放这里。
    """
    return {
        META_DIRECTION_RAW: result.direction_raw,
        META_DIRECTION_HITS: result.direction_hits,
        META_TIMING_RAW: result.timing_raw,
        META_READY: result.ready,
    }


def persist_skill_snapshots(
    session: Session,
    results: Sequence[AuthorSkillResult],
) -> int:
    """幂等写入 ``author_skill_snapshots``，返回本次实际新增的行数。

    幂等键 = ``(author_id, as_of)``：已存在则跳过（append-only 守卫禁止 UPDATE，
    重复运行同一 ``as_of`` 不产生重复行）。
    """
    written = 0
    for result in results:
        author_id = uuid.UUID(result.author_id)
        exists = session.scalar(
            sa.select(AuthorSkillSnapshot.id).where(
                AuthorSkillSnapshot.author_id == author_id,
                AuthorSkillSnapshot.as_of == result.as_of,
            )
        )
        if exists is not None:
            continue
        session.add(
            AuthorSkillSnapshot(
                author_id=author_id,
                as_of=result.as_of,
                direction_skill=_to_decimal(result.direction_skill),
                timing_skill=_to_decimal(result.timing_skill),
                entry_skill=_to_decimal(result.entry_skill),
                exit_skill=_to_decimal(result.exit_skill),
                independence_score=_to_decimal(result.independence_score),
                calibration_score=_to_decimal(result.calibration_score),
                marginal_alpha=None,
                sample_size=result.sample_size,
                metadata_json=build_skill_metadata(result),
            )
        )
        written += 1
    return written


def _to_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _to_decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _enum_value(value: Any) -> str | None:
    return None if value is None else value.value
