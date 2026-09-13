"""观点抽取接口（Phase 2 Author Lab）。

对应文档：
- `docs/10_标注规范.md`（已批准 v1.0）：§4 判定规则、§5 严格 JSON Schema、§7 验收指标
- `docs/07` Phase 2 Prompt：Post → Opinion 解析管线；所有输出必须记录 parser / model version
- `docs/02` §5.2：Processor 输出不得覆盖原始数据，必须可追溯（`processor_name + version`）

设计原则：
1. **纯函数式组件**：输入文本（+ 是否含媒体的提示），输出结构化 draft 与诊断；
   不访问数据库、不访问网络、**不读取当前时间** —— 因此可离线复现、可 100% Mock 测试。
2. **归属与时间由管道注入**：`author_id` / `author_post_id` / `effective_at` 不在本层生成
   （禁止模型编造时间与归属，见 `docs/10 §5` 契约细节 1）。
3. **无法判定必须显式表达**：`stance=UNKNOWN` + rationale 标记（conditional / quote-only /
   post-hoc），或返回"无观点"；**禁止**用默认值（0、XAUUSD）冒充。
4. **失败不静默**：校验不通过的候选进入 `diagnostics`，供数据质量巡检（`docs/08 §13`
   的 Parser Failure Rate）。
5. **接口可替换**：真实 LLM 提取器只需实现同一 `OpinionExtractor` 协议（默认仍走 Mock）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from src.processors.schemas import AuthorOpinionDraft

__all__ = [
    "DiagnosticCode",
    "ExtractionDiagnostic",
    "OpinionExtractionResult",
    "OpinionExtractor",
    "build_drafts",
]


class DiagnosticCode(StrEnum):
    """抽取诊断码（数据质量与人工复核的依据，禁止用自由文本代替）。"""

    EMPTY_TEXT = "empty_text"
    IMAGE_ONLY = "image_only"
    NO_STANCE_KEYWORD = "no_stance_keyword"
    CONDITIONAL = "conditional"
    QUOTE_ONLY = "quote-only"
    POST_HOC = "post-hoc"
    NEGATED_DIRECTION = "negated_direction"
    WEAK_LANGUAGE = "weak_language"
    PRICE_DROPPED = "price_dropped"
    UNKNOWN_INSTRUMENT = "unknown_instrument"
    INVALID_DRAFT = "invalid_draft"
    #: LLM 侧：API 调用最终失败（超时 / 429 / 5xx 重试耗尽）——单帖降级，不中断整批
    LLM_API_ERROR = "llm_api_error"
    #: LLM 侧：模型输出不是可解析的 JSON 结构
    LLM_PARSE_ERROR = "llm_parse_error"


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostic:
    """一条抽取诊断（原因 + 人类可读说明 + 原文片段）。"""

    code: DiagnosticCode
    message: str
    snippet: str | None = None

    def summary(self) -> str:
        suffix = f" | 片段：{self.snippet}" if self.snippet else ""
        return f"[{self.code.value}] {self.message}{suffix}"


@dataclass(frozen=True, slots=True)
class OpinionExtractionResult:
    """一次文本抽取的完整结果。

    ``drafts`` 为空 = **本帖无观点**（对应人工标注的 `no_opinion=true`），
    与"有观点但方向判不出"（`stance=UNKNOWN` 的 draft）是**两种不同情况**，不得混用。
    """

    parser_version: str
    drafts: tuple[AuthorOpinionDraft, ...] = ()
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def no_opinion(self) -> bool:
        return not self.drafts

    @property
    def unknown_stance_count(self) -> int:
        """UNKNOWN 观点数（用于 `docs/10 §7` 的拒答率报告）。"""
        return sum(1 for draft in self.drafts if draft.stance.value == "UNKNOWN")

    def summary(self) -> str:
        codes = ",".join(sorted({diagnostic.code.value for diagnostic in self.diagnostics}))
        return (
            f"{self.parser_version}: drafts={len(self.drafts)} "
            f"unknown={self.unknown_stance_count} warnings={len(self.warnings)} "
            f"diagnostics=[{codes}]"
        )


@runtime_checkable
class OpinionExtractor(Protocol):
    """观点抽取器协议（Mock 与真实 LLM 实现共用）。

    实现必须满足：
    - ``parser_version`` 为稳定字符串（入库到 `author_opinions.parser_version`）；
    - ``extract`` 为**确定性**函数（同输入同输出，便于复现与验收比对）；
    - 不抛异常表达"没有观点"（用空 `drafts` + diagnostics 表达）。
    """

    parser_version: str

    def extract(self, text: str, *, has_media: bool = False) -> OpinionExtractionResult:
        """从文本抽取 0..N 条观点。"""
        ...


def build_drafts(
    candidates: Iterable[Mapping[str, Any]], *, parser_version: str
) -> tuple[tuple[AuthorOpinionDraft, ...], tuple[ExtractionDiagnostic, ...]]:
    """把"原始候选（dict）"校验成契约 draft，非法候选转成诊断。

    供 Mock 与未来 LLM 提取器共用：LLM 返回的 JSON 必须经过同一层校验，
    任何多余字段 / 越界数值都会被拒绝，而不是写进数据库才报错。

    Args:
        candidates: 候选字典序列（键对齐 `docs/10 §5` JSON Schema）。
        parser_version: 解析器版本；候选自身缺失或被改写时以本参数为准。

    Returns:
        ``(drafts, diagnostics)``；`diagnostics` 只包含 `INVALID_DRAFT`。
    """

    drafts: list[AuthorOpinionDraft] = []
    diagnostics: list[ExtractionDiagnostic] = []
    for index, candidate in enumerate(candidates):
        payload: dict[str, Any] = dict(candidate)
        payload.setdefault("parser_version", parser_version)
        try:
            drafts.append(AuthorOpinionDraft(**payload))
        except ValidationError as exc:
            diagnostics.append(
                ExtractionDiagnostic(
                    code=DiagnosticCode.INVALID_DRAFT,
                    message=f"第 {index + 1} 条候选不满足观点契约：{exc.error_count()} 处错误",
                    snippet=str(payload.get("rationale") or payload)[:200],
                )
            )
    return tuple(drafts), tuple(diagnostics)