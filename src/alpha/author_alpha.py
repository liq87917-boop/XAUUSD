"""Phase 3.3 Author Alpha 的离线技能计算框架。

定位与边界（.clinerules 预测/策略/风险解耦红线）：
- 纯函数：输入观点评估样本，输出各维度技能分 + 样本量；**不读数据库、不写数据库**。
- **不产出权重**：``author_weight_snapshots`` 由后续管线在样本达标时另行计算，
  本模块只负责 ``author_skill_snapshots`` 所需的技能值。
- 不含仓位、交易、手续费、风控逻辑。

技能维度口径（第一版，供审阅）：
- ``direction_skill``：方向命中率，Beta(1,1) 后验均值收缩（小样本向 0.5 收缩，
  与 ``docs/05 Phase 2``「样本量修正，不能简单用胜率作为总分」一致）；
- ``timing_skill``：方向可评价样本（LONG/SHORT 已判定）中有向 log_return
  （``stance_direction × log_return``）> 0 的比例（天然 [0,1]，无需 sigmoid）；
  原始均值另存 ``timing_raw`` 供报告披露；
- ``calibration_score``：1 - ECE（5 桶等频），confidence 样本不足时返回 None；
- ``entry_skill`` / ``exit_skill`` / ``independence_score``：第一版返回 None
  （需要 ``entry_low/entry_high/stop_loss/take_profit`` 与传播图数据，
  ``OpinionLabel`` 未携带）。

防泄漏：``compute_author_skill`` 内部按 ``as_of`` 过滤，只统计
``effective_at <= as_of`` 的样本（严禁用未来观点算历史技能）。

硬门槛：方向可评价样本数 < ``MIN_AUTHOR_SAMPLES`` 时 ``ready=False``，
调用方据此不写 ``author_weight_snapshots``（``docs/14 §3.3`` 验收标准 1）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

MIN_AUTHOR_SAMPLES: Final[int] = 30
DIRECTION_PRIOR_A: Final[float] = 1.0
DIRECTION_PRIOR_B: Final[float] = 1.0
CALIBRATION_BINS: Final[int] = 5
MIN_CALIBRATION_SAMPLES: Final[int] = 10
#: stance -> 有向符号；FLAT/UNKNOWN 不参与方向技能（方向未给出，不猜）。
STANCE_DIRECTION: Final[dict[str, int]] = {"LONG": 1, "SHORT": -1, "FLAT": 0, "UNKNOWN": 0}


@dataclass(frozen=True, slots=True)
class SkillSample:
    """一条用于技能评估的观点记录（已合并标签结果与观点原始字段）。

    ``effective_at`` 必须是带时区的 UTC 时刻（由组装方保证，见 ``_ensure_aware``）。
    """

    opinion_id: str
    effective_at: datetime
    stance_direction: int
    horizon: str | None = None
    information_type: str | None = None
    confidence: float | None = None
    direction_hit: bool | None = None
    log_return: float | None = None
    entry_lag_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AuthorSkillResult:
    """作者技能快照的计算结果（不落库）。"""

    author_id: str
    as_of: datetime
    sample_size: int
    direction_hits: int
    direction_raw: float | None
    direction_skill: float | None
    timing_raw: float | None
    timing_skill: float | None
    calibration_score: float | None
    entry_skill: float | None
    exit_skill: float | None
    independence_score: float | None
    ready: bool


def _ensure_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(UTC)


def filter_by_time(samples: Sequence[SkillSample], as_of: datetime) -> tuple[SkillSample, ...]:
    """只保留 ``effective_at <= as_of`` 的样本（防未来数据泄漏）。"""
    moment = _ensure_aware(as_of, "as_of")
    return tuple(
        sample
        for sample in samples
        if _ensure_aware(sample.effective_at, "sample.effective_at") <= moment
    )


def beta_posterior_mean(hits: int, trials: int, prior_a: float, prior_b: float) -> float:
    """Beta-Binomial 后验均值收缩：``(hits + a) / (trials + a + b)``。

    弱先验 ``a=b=1``（均匀分布）时，小样本向 0.5 收缩；样本越多越接近原始命中率。
    """
    if trials < 0 or hits < 0 or hits > trials:
        raise ValueError(f"非法命中计数：hits={hits} trials={trials}")
    if prior_a <= 0 or prior_b <= 0:
        raise ValueError("Beta 先验参数必须 > 0")
    return (hits + prior_a) / (trials + prior_a + prior_b)


def _directional_samples(
    samples: Sequence[SkillSample],
) -> tuple[SkillSample, ...]:
    """方向可评价样本：stance 有明确方向（LONG/SHORT）且方向已判定。"""
    return tuple(
        sample
        for sample in samples
        if sample.stance_direction != 0 and sample.direction_hit is not None
    )


def compute_direction_skill(
    samples: Sequence[SkillSample],
    prior_a: float = DIRECTION_PRIOR_A,
    prior_b: float = DIRECTION_PRIOR_B,
) -> tuple[int, int, float | None, float | None]:
    """返回 ``(hits, trials, raw, shrunken)``。

    ``raw`` 为原始命中率，``shrunken`` 为 Beta 后验均值收缩后的方向技能值。
    """
    usable = _directional_samples(samples)
    trials = len(usable)
    hits = sum(1 for sample in usable if sample.direction_hit is True)
    if trials == 0:
        return hits, trials, None, None
    raw = hits / trials
    shrunken = beta_posterior_mean(hits, trials, prior_a, prior_b)
    return hits, trials, raw, shrunken


def compute_timing_skill(
    samples: Sequence[SkillSample],
) -> tuple[float | None, float | None]:
    """返回 ``(raw_mean, skill)``。

    ``raw_mean`` 为方向可评价样本（LONG/SHORT 已判定）的平均有向 log_return
    （``stance_direction × log_return``，> 0 表示看对方向且赚钱）；
    ``skill`` 为其中有向收益 > 0 的比例（天然 [0,1]，无需 sigmoid）。
    无可评价样本时返回 ``(None, None)``。
    """
    directed = [
        sample.stance_direction * sample.log_return
        for sample in samples
        if sample.stance_direction != 0
        and sample.direction_hit is not None
        and sample.log_return is not None
    ]
    if not directed:
        return None, None
    raw_mean = sum(directed) / len(directed)
    skill = sum(1 for value in directed if value > 0) / len(directed)
    return raw_mean, skill


def compute_calibration_score(
    samples: Sequence[SkillSample],
    n_bins: int = CALIBRATION_BINS,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
) -> float | None:
    """返回 ``1 - ECE``（期望校准误差的补，[0,1]）。

    只用方向已判定且 confidence 给出的样本；样本数 < ``min_samples`` 时返回 None。
    ECE 用等频分桶，逐桶算 ``|平均 confidence - 实际命中率|`` 的加权平均。
    """
    if n_bins <= 0 or min_samples <= 0:
        raise ValueError("n_bins 与 min_samples 必须 > 0")
    usable: list[tuple[SkillSample, float]] = []
    for sample in samples:
        if sample.direction_hit is not None and sample.confidence is not None:
            usable.append((sample, sample.confidence))
    if len(usable) < min_samples:
        return None
    ordered = sorted(usable, key=lambda pair: pair[1])
    bucket_size = len(ordered) / n_bins
    ece = 0.0
    for index in range(n_bins):
        start = int(round(index * bucket_size))
        end = int(round((index + 1) * bucket_size))
        bucket = ordered[start:end]
        if not bucket:
            continue
        mean_confidence = sum(pair[1] for pair in bucket) / len(bucket)
        observed_rate = sum(1 for pair in bucket if pair[0].direction_hit is True) / len(bucket)
        ece += (len(bucket) / len(ordered)) * abs(mean_confidence - observed_rate)
    return max(0.0, 1.0 - ece)


def compute_author_skill(
    samples: Sequence[SkillSample],
    *,
    author_id: str,
    as_of: datetime,
    min_samples: int = MIN_AUTHOR_SAMPLES,
    direction_prior_a: float = DIRECTION_PRIOR_A,
    direction_prior_b: float = DIRECTION_PRIOR_B,
) -> AuthorSkillResult:
    """时间外推 + 样本量修正 + 硬门槛，返回技能结果（不落库）。

    - 时间外推：只统计 ``effective_at <= as_of`` 的样本（防泄漏）；
    - 样本量修正：direction 用 Beta 后验均值收缩；
    - 硬门槛：方向可评价样本数 < ``min_samples`` 时 ``ready=False``。
    """
    moment = _ensure_aware(as_of, "as_of")
    visible = filter_by_time(samples, as_of)
    hits, trials, raw, shrunken = compute_direction_skill(
        visible, direction_prior_a, direction_prior_b
    )
    timing_raw, timing_skill = compute_timing_skill(visible)
    calibration = compute_calibration_score(visible)
    return AuthorSkillResult(
        author_id=author_id,
        as_of=moment,
        sample_size=trials,
        direction_hits=hits,
        direction_raw=raw,
        direction_skill=shrunken,
        timing_raw=timing_raw,
        timing_skill=timing_skill,
        calibration_score=calibration,
        # 第一版不计算：需要 entry_low/entry_high/stop_loss/take_profit 与传播图数据。
        entry_skill=None,
        exit_skill=None,
        independence_score=None,
        ready=trials >= min_samples,
    )
