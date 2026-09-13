"""Post → Opinion 管道骨架（Phase 2；**纯 Mock，不接真实 LLM**）。

链路（对应 `docs/02 §5.2` 的 Processor 链与 `docs/05` Phase 2）：

```text
author_posts（+ raw_items）
    → [OpinionExtractor]（当前 = RegexOpinionExtractor，纯 Mock）
    → processed_items（append-only：processor_name / processor_version / status / structured_json）
    → author_opinions（结构化观点，带 parser_version 与 effective_at）
```

设计要点（逐条对齐红线与文档）：

1. **不碰原始层**：只读 `author_posts` / `raw_items`，只写 `processed_items` 与
   `author_opinions`（两者均为 append-only 事实表，受不可覆盖守卫保护）。
2. **幂等**：`processed_items` 的唯一键
   ``(raw_item_id, processor_name, processor_version)`` + 处理前存在性检查
   → 重复运行不产生重复观点；调用方也可用 ``limit`` 分批处理。
3. **时间因果**：``opinion.effective_at = post.effective_at``
   （:mod:`src.processors.timeline`），写入前用
   :func:`assert_opinion_available_after_post` 复核；数据库侧另有
   ``created_at >= effective_at`` 的 CHECK（migration 0005）兜底。
4. **失败不静默**：单个帖子抽取异常 → 该帖子写 ``processed_items.status = FAILED``
   + ``error_message``，其余帖子照常处理（与采集器"单源失败隔离"同一原则），并在报告中计数。
5. **无观点也留痕**：抽不到观点时仍写 ``processed_items``（``SUCCESS`` +
   ``structured_json.no_opinion = true``），避免每轮重复解析同一条帖子；
   文本为空则写 ``SKIPPED`` 并说明原因。
6. **不臆造**：只处理 `author_posts` 已归属的帖子；标的必须在 `instruments` 白名单内，
   否则**跳过该条观点**并计数（不静默丢弃，也不自动造标的）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import AuthorOpinion, AuthorPost, Instrument, ProcessedItem, RawItem
from database.models.enums import ProcessStatus
from src.processors.opinion_extractor import (
    DiagnosticCode,
    OpinionExtractionResult,
    OpinionExtractor,
)
from src.processors.regex_extractor import RegexOpinionExtractor
from src.processors.schemas import AuthorOpinionDraft
from src.processors.timeline import (
    assert_opinion_available_after_post,
    ensure_utc_from_database,
    resolve_opinion_effective_at,
)

__all__ = ["OpinionPipeline", "PipelineReport", "iter_draft_instruments", "resolve_instrument_id"]

_log = get_logger("processors.opinion_pipeline")

#: Processor 名称（写入 `processed_items.processor_name`）
PROCESSOR_NAME = "opinion_extractor"
#: 文本为空时的跳过原因（写入 structured_json，便于数据质量巡检）
REASON_EMPTY_TEXT = "empty_text"


@dataclass(slots=True)
class PipelineReport:
    """一次管道运行的可观测结果。"""

    posts_scanned: int = 0
    posts_processed: int = 0
    posts_no_opinion: int = 0
    posts_skipped_empty_text: int = 0
    posts_failed: int = 0
    opinions_created: int = 0
    opinions_skipped_unknown_instrument: int = 0
    diagnostics: dict[str, int] = field(default_factory=dict)

    def count_diagnostic(self, code: DiagnosticCode, *, amount: int = 1) -> None:
        self.diagnostics[code.value] = self.diagnostics.get(code.value, 0) + amount

    def summary(self) -> str:
        return (
            f"scanned={self.posts_scanned} processed={self.posts_processed} "
            f"no_opinion={self.posts_no_opinion} empty={self.posts_skipped_empty_text} "
            f"failed={self.posts_failed} opinions={self.opinions_created} "
            f"unknown_instrument={self.opinions_skipped_unknown_instrument}"
        )


def resolve_instrument_id(
    symbol: str | None, instrument_map: dict[str, uuid.UUID]
) -> uuid.UUID | None:
    """把抽取器给出的标的代码解析为 ``instruments.id``（纯函数，便于单测）。

    - ``symbol is None`` → ``None``（观点未指向标的，合法情形）；
    - 代码不在白名单 → 同样返回 ``None``，调用方必须计数并记录诊断（不得自动造标的）。
    """
    if symbol is None:
        return None
    return instrument_map.get(symbol.strip().upper())


def iter_draft_instruments(drafts: Iterable[AuthorOpinionDraft]) -> list[str]:
    """返回 drafts 中出现的标的代码（去重、保持顺序；便于数据质量巡检）。"""
    seen: dict[str, None] = {}
    for draft in drafts:
        if draft.instrument is not None:
            seen.setdefault(draft.instrument, None)
    return list(seen)


class OpinionPipeline:
    """`author_posts` → `processed_items` → `author_opinions` 的单批处理管道。"""

    def __init__(
        self,
        session: Session,
        *,
        extractor: OpinionExtractor | None = None,
    ) -> None:
        self._session = session
        self._extractor: OpinionExtractor = extractor or RegexOpinionExtractor()

    @property
    def processor_name(self) -> str:
        return PROCESSOR_NAME

    @property
    def processor_version(self) -> str:
        return self._extractor.parser_version

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, *, limit: int | None = None) -> PipelineReport:
        """处理一批尚未被本 processor_version 处理过的帖子。

        Args:
            limit: 本次最多处理多少条帖子（``None`` = 全部待处理）。

        Returns:
            :class:`PipelineReport`（含诊断计数与失败计数）。
        """
        report = PipelineReport()
        instrument_map = self._instrument_map()
        pending = self._pending_posts(limit=limit)
        report.posts_scanned = len(pending)

        for post, _raw_item in pending:
            # 数据库读回的时间统一为 UTC-aware（SQLite 不保存偏移；PG 读回即 aware）
            post_effective_at = ensure_utc_from_database(
                post.effective_at, field_name="author_posts.effective_at"
            )
            if not (post.text_content or "").strip():
                self._record_skip(post, reason=REASON_EMPTY_TEXT, effective_at=post_effective_at)
                report.posts_skipped_empty_text += 1
                continue

            try:
                result = self._extractor.extract(post.text_content or "", has_media=post.has_media)
            except Exception as exc:  # noqa: BLE001 - 单帖失败隔离：记录而非崩溃
                self._record_failure(post, exc, effective_at=post_effective_at)
                report.posts_failed += 1
                _log.warning("帖子 %s 抽取失败：%s: %s", post.id, type(exc).__name__, exc)
                continue

            self._write_processed_item(post, result, report, effective_at=post_effective_at)
            created = self._write_opinions(
                post, result, instrument_map, report, effective_at=post_effective_at
            )
            report.posts_processed += 1
            report.opinions_created += created
            if result.no_opinion:
                report.posts_no_opinion += 1

        self._session.flush()
        _log.info(
            "Post→Opinion 管道完成 | processor=%s@%s | %s",
            self.processor_name,
            self.processor_version,
            report.summary(),
        )
        return report

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def _instrument_map(self) -> dict[str, uuid.UUID]:
        rows = self._session.execute(sa.select(Instrument.symbol, Instrument.id)).all()
        return {str(symbol).upper(): instrument_id for symbol, instrument_id in rows}

    def _pending_posts(self, *, limit: int | None) -> list[tuple[AuthorPost, RawItem]]:
        """尚未被本 processor_version 处理过的帖子（按 `effective_at` 顺序 = 时间安全）。"""
        processed = sa.select(ProcessedItem.raw_item_id).where(
            ProcessedItem.processor_name == self.processor_name,
            ProcessedItem.processor_version == self.processor_version,
        )
        statement = (
            sa.select(AuthorPost, RawItem)
            .join(RawItem, RawItem.id == AuthorPost.raw_item_id)
            .where(AuthorPost.raw_item_id.not_in(processed))
            .order_by(AuthorPost.effective_at, AuthorPost.id)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return [(post, raw) for post, raw in self._session.execute(statement).all()]

    # ------------------------------------------------------------------
    # 写入（processed_items 先于 author_opinions：先留痕再产事实）
    # ------------------------------------------------------------------
    def _record_skip(self, post: AuthorPost, *, reason: str, effective_at: datetime) -> None:
        self._session.add(
            ProcessedItem(
                raw_item_id=post.raw_item_id,
                processor_name=self.processor_name,
                processor_version=self.processor_version,
                structured_json={"status_reason": reason},
                status=ProcessStatus.SKIPPED,
                effective_at=effective_at,
            )
        )

    def _record_failure(
        self, post: AuthorPost, exc: Exception, *, effective_at: datetime
    ) -> None:
        """失败留痕：`processed_items` **没有** error_message 列（schema 已冻结），
        因此把异常类型与消息写入 `structured_json`（数据质量巡检按 code/exception 统计）。
        """
        message = f"{type(exc).__name__}: {exc}"
        self._session.add(
            ProcessedItem(
                raw_item_id=post.raw_item_id,
                processor_name=self.processor_name,
                processor_version=self.processor_version,
                structured_json={
                    "exception": type(exc).__name__,
                    "error_message": message,
                },
                status=ProcessStatus.FAILED,
                effective_at=effective_at,
            )
        )

    def _write_processed_item(
        self,
        post: AuthorPost,
        result: OpinionExtractionResult,
        report: PipelineReport,
        *,
        effective_at: datetime,
    ) -> None:
        """写入加工结果（append-only；`normalized_text` 留空——本处理器不做文本归一化）。"""
        for diagnostic in result.diagnostics:
            report.count_diagnostic(diagnostic.code)
        self._session.add(
            ProcessedItem(
                raw_item_id=post.raw_item_id,
                processor_name=self.processor_name,
                processor_version=self.processor_version,
                structured_json={
                    "no_opinion": result.no_opinion,
                    "warnings": list(result.warnings),
                    "diagnostics": [
                        {
                            "code": diagnostic.code.value,
                            "message": diagnostic.message,
                            "snippet": diagnostic.snippet,
                        }
                        for diagnostic in result.diagnostics
                    ],
                    "drafts": [draft.to_json_schema_example() for draft in result.drafts],
                },
                status=ProcessStatus.SUCCESS,
                effective_at=effective_at,
            )
        )

    def _write_opinions(
        self,
        post: AuthorPost,
        result: OpinionExtractionResult,
        instrument_map: dict[str, uuid.UUID],
        report: PipelineReport,
        *,
        effective_at: datetime,
    ) -> int:
        """把 drafts 写成 `author_opinions`；未知标的跳过该条并计数。"""
        created = 0
        opinion_effective_at = resolve_opinion_effective_at(post_effective_at=effective_at)
        assert_opinion_available_after_post(
            opinion_effective_at=opinion_effective_at, post_effective_at=effective_at
        )
        for draft in result.drafts:
            instrument_id = resolve_instrument_id(draft.instrument, instrument_map)
            if draft.instrument is not None and instrument_id is None:
                report.opinions_skipped_unknown_instrument += 1
                report.count_diagnostic(DiagnosticCode.UNKNOWN_INSTRUMENT)
                _log.warning(
                    "跳过观点：标的 %s 不在 instruments 白名单（帖子 %s）",
                    draft.instrument,
                    post.id,
                )
                continue

            self._session.add(
                self._build_opinion(
                    post=post,
                    draft=draft,
                    instrument_id=instrument_id,
                    effective_at=opinion_effective_at,
                )
            )
            created += 1
        return created

    def _build_opinion(
        self,
        *,
        post: AuthorPost,
        draft: AuthorOpinionDraft,
        instrument_id: uuid.UUID | None,
        effective_at: datetime,
    ) -> AuthorOpinion:
        return AuthorOpinion(
            author_id=post.author_id,
            author_post_id=post.id,
            stance=draft.stance,
            instrument_id=instrument_id,
            horizon=draft.horizon,
            confidence=draft.confidence,
            entry_low=draft.entry_low,
            entry_high=draft.entry_high,
            stop_loss=draft.stop_loss,
            take_profit=draft.take_profit,
            information_type=draft.information_type,
            rationale=draft.rationale,
            parser_version=draft.parser_version,
            effective_at=effective_at,
        )