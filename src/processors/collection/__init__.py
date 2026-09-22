"""采集后处理流水线子包（TD-11）。

模块分工（职责边界清晰，禁止互相越界）：
- ``contracts``：输入/输出契约、状态枚举、采集接线协议（``PersistedItemProcessor``）；
- ``normalize``：文本归一化（NFKC / 零宽字符 / 空白折叠 / 截断）；
- ``identity``：内容指纹、幂等键、去重索引；
- ``validate``：数据质量校验（坏数据返回 issues，不抛异常打断整批）；
- ``pipeline``：``CollectionProcessor``（四阶段确定性流水线 + ``processed_items`` 幂等落库）。

本子包**不联网**：不导入 HTTP transport / aiohttp / registry，不读 robots、不做 provider
授权、不解析站点、不调度。事实时间只来自入参，绝不用"当前时间"推断。
"""

from src.processors.collection.contracts import (
    AUDIT_PIPELINE_STAGES,
    DEFAULT_FUTURE_TOLERANCE,
    DEFAULT_MAX_TEXT_CHARS,
    PROCESSOR_NAME,
    PROCESSOR_VERSION,
    BatchStatus,
    PersistedItemProcessor,
    ProcessedRecord,
    ProcessingReport,
    ProcessorInput,
    RawItemLike,
    RecordOutcome,
)
from src.processors.collection.identity import (
    DedupIndex,
    content_fingerprint,
    identity_key,
)
from src.processors.collection.normalize import normalize_text, truncate_text
from src.processors.collection.pipeline import (
    REASON_DUPLICATE,
    REASON_EMPTY_CONTENT,
    REASON_INVALID_TIMESTAMP,
    REASON_MISSING_SOURCE_RECORD_ID,
    REASON_PUBLISHED_AT_IN_FUTURE,
    CollectionProcessor,
    batch_status_for,
)
from src.processors.collection.validate import (
    ValidationIssue,
    format_issues,
    validate_normalized_record,
)

__all__ = [
    "AUDIT_PIPELINE_STAGES",
    "DEFAULT_FUTURE_TOLERANCE",
    "DEFAULT_MAX_TEXT_CHARS",
    "PROCESSOR_NAME",
    "PROCESSOR_VERSION",
    "REASON_DUPLICATE",
    "REASON_EMPTY_CONTENT",
    "REASON_INVALID_TIMESTAMP",
    "REASON_MISSING_SOURCE_RECORD_ID",
    "REASON_PUBLISHED_AT_IN_FUTURE",
    "BatchStatus",
    "CollectionProcessor",
    "DedupIndex",
    "PersistedItemProcessor",
    "ProcessedRecord",
    "ProcessingReport",
    "ProcessorInput",
    "RawItemLike",
    "RecordOutcome",
    "ValidationIssue",
    "batch_status_for",
    "content_fingerprint",
    "format_issues",
    "identity_key",
    "normalize_text",
    "truncate_text",
    "validate_normalized_record",
]
