"""Phase 2 加工层（Processor）：把原始/归属数据转成结构化研究事实。

当前内容（Phase 2 第一步）：
- ``schemas``：观点抽取数据契约（严格 JSON Schema，禁止多余字段/越界数值）
- ``opinion_extractor``：抽取器协议 + 共享校验工具（``OpinionExtractor`` / ``build_drafts``）
- ``regex_extractor``：Mock 实现 ``RegexOpinionExtractor``（`parser_version = "mock-regex-v1"`）
- ``timeline``：观点时间因果契约（`effective_at >= author_posts.effective_at`，团队裁决 2）
- ``opinion_pipeline``：Post → Opinion 管道骨架（`author_posts` → `processed_items` →
  `author_opinions`，纯 Mock、幂等、失败隔离）

约定：
- Processor 只写 ``processed_items`` / ``author_opinions``（append-only），
  **绝不修改原始层**（03_数据库完整设计 §10）；
- 每个处理器都必须写入 ``processor_name + processor_version``，结果可追溯、可复现；
- 抽取器不访问网络、不读取当前时间（Mock 与真实 LLM 实现都遵守，见 docs/10 §5）。
"""

from src.processors.opinion_extractor import (
    DiagnosticCode,
    ExtractionDiagnostic,
    OpinionExtractionResult,
    OpinionExtractor,
    build_drafts,
)
from src.processors.opinion_pipeline import (
    PROCESSOR_NAME,
    OpinionPipeline,
    PipelineReport,
    iter_draft_instruments,
    resolve_instrument_id,
)
from src.processors.propagation import (
    PropagationCluster,
    PropagationDetector,
    PropagationDocument,
    PropagationEdgeCandidate,
    PropagationResult,
    store_propagation_edges,
)
from src.processors.regex_extractor import DEFAULT_PARSER_VERSION, RegexOpinionExtractor
from src.processors.schemas import AuthorOpinionDraft
from src.processors.similarity import TfidfCosineModel, tokenize
from src.processors.timeline import (
    assert_opinion_available_after_post,
    ensure_utc_from_database,
    resolve_opinion_effective_at,
)

__all__ = [
    "DEFAULT_PARSER_VERSION",
    "PROCESSOR_NAME",
    "AuthorOpinionDraft",
    "DiagnosticCode",
    "ExtractionDiagnostic",
    "OpinionExtractionResult",
    "OpinionExtractor",
    "OpinionPipeline",
    "PipelineReport",
    "PropagationCluster",
    "PropagationDetector",
    "PropagationDocument",
    "PropagationEdgeCandidate",
    "PropagationResult",
    "RegexOpinionExtractor",
    "TfidfCosineModel",
    "assert_opinion_available_after_post",
    "build_drafts",
    "ensure_utc_from_database",
    "iter_draft_instruments",
    "resolve_instrument_id",
    "resolve_opinion_effective_at",
    "store_propagation_edges",
    "tokenize",
]