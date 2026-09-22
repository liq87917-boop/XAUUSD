"""Evidence Intake 引擎：validate-first / dry-run-first 的授权证据导入（GOLD-005）。

流程（**默认 dry-run，默认零写入**）：

```text
输入文件（JSONL / CSV，本地，零网络）
  → ① 读取与格式识别（怀疑的格式 / 无法解析的行单列隔离，不静默丢弃）
  → ② 逐行机械校验（src/evidence/validation.py，纯函数）
  → ③ 幂等判定（同 (source, source_record_id) 内容一致 → DUPLICATE；
        内容不一致 → IDENTITY_CONFLICT 隔离，绝不 UPDATE 覆盖）
  → ④ 仅在显式 --no-dry-run 时：append-only 写入 raw_items +
        processed_items（经现有 CollectionProcessor，processor 版本留痕）
  → ⑤ 报告 / manifest / quarantine（只含脱敏字段与稳定原因码）
```

不变量：

- **dry-run 零写入**：不写库、不建 source、不落 manifest / quarantine 文件；
- **不夸大资格**：``published_at`` 早于 ``collected_at`` 不等于"历史可用"；
  没有独立 ``available_at`` 证据的记录标记 ``NOT_OOS_ELIGIBLE``（``AVAILABILITY_UNPROVEN``）；
- **授权不可推断**：只有显式 ``APPROVED`` + 白名单依据 + 可核验引用 + 三项许可 + 人工签认
  才会被记为可信证据；缺失 / UNKNOWN / DENIED 一律隔离；
- **不新增来源采集能力**：自动创建的 ``sources`` 行一律 ``enabled=false`` 且不写
  ``config_json``（不注册采集器），不会被 Scheduler 采集；来源启用仍需人工 Gate。
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.models import RawItem, Source
from database.models.enums import RawItemType, SourceType
from src.evidence.contracts import (
    EVIDENCE_CONTRACT_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    EvidenceScope,
    ReasonCode,
    RowStatus,
)
from src.evidence.validation import EvidenceRecord, RowAssessment, assess_row
from src.processors.collection.pipeline import CollectionProcessor

__all__ = [
    "InputFile",
    "InputRow",
    "IntakeCounts",
    "RowOutcome",
    "EvidenceIntakeReport",
    "intake_evidence",
    "load_input_rows",
    "read_input_file",
    "resolve_format",
]

#: 支持的输入格式（标准库即可处理，不新增第三方依赖）
SUPPORTED_FORMATS: Final[tuple[str, ...]] = ("jsonl", "csv")
#: 后缀 → 格式
_SUFFIX_FORMATS: Final[dict[str, str]] = {
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".json": "jsonl",
    ".csv": "csv",
}


@dataclass(frozen=True, slots=True)
class InputFile:
    """输入文件的可审计标识（路径 + 格式 + SHA-256）。"""

    path: str
    format: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "format": self.format, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class InputRow:
    """一行输入（``data is None`` 表示该行无法解析，将按行隔离）。"""

    row_number: int
    data: Mapping[str, Any] | None
    error: str | None = None

    @property
    def readable(self) -> bool:
        return self.data is not None


def resolve_format(path: Path, requested: str) -> str:
    """解析 ``--format``（``auto`` 按后缀；未知后缀拒绝，不猜）。"""
    name = requested.strip().lower()
    if name == "auto":
        suffix = path.suffix.lower()
        resolved = _SUFFIX_FORMATS.get(suffix)
        if resolved is None:
            raise ValueError(
                f"无法按后缀识别输入格式：{path.name}；请用 --format 显式声明 "
                f"{SUPPORTED_FORMATS}"
            )
        return resolved
    if name not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的 --format：{requested!r}；可选 {('auto', *SUPPORTED_FORMATS)}")
    return name


def read_input_file(
    path: Path, *, requested_format: str = "auto"
) -> tuple[InputFile, list[InputRow]]:
    """读取输入文件（本地只读）：返回可审计文件标识与逐行数据。

    输入为空（没有任何数据行）不是错误：由调用方决定（CLI 以退出码 3 表示）。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 后缀无法识别 / ``--format`` 非法 / CSV 无表头。
    """
    if not path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    fmt = resolve_format(path, requested_format)
    payload = path.read_bytes()
    rows = load_input_rows(path, fmt=fmt)
    return (
        InputFile(path=str(path), format=fmt, sha256=hashlib.sha256(payload).hexdigest()),
        rows,
    )


def load_input_rows(path: Path, *, fmt: str) -> list[InputRow]:
    """按格式读取逐行数据（格式错的行单列隔离，不静默丢弃）。"""
    if fmt == "csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = [name for name in (reader.fieldnames or []) if name is not None]
            if not headers:
                raise ValueError(f"CSV 没有表头：{path}")
            return [
                InputRow(
                    row_number=number,
                    data={key: value for key, value in row.items() if key is not None},
                )
                for number, row in enumerate(reader, start=2)
            ]
    rows: list[InputRow] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            rows.append(InputRow(number, None, f"JSON 解析失败：{exc.msg}（行内容不记录）"))
            continue
        if not isinstance(parsed, dict):
            rows.append(InputRow(number, None, "JSONL 每行必须是对象（object）"))
            continue
        rows.append(InputRow(number, parsed))
    return rows


@dataclass(frozen=True, slots=True)
class RowOutcome:
    """逐行结论（机器可读；只含脱敏字段与稳定原因码）。"""

    index: int
    row_number: int
    status: RowStatus
    reason_codes: tuple[ReasonCode, ...]
    reasons: tuple[str, ...]
    fingerprint: str | None
    source: str
    source_record_id: str
    oos_eligible: bool
    not_oos_eligible_reason: ReasonCode | None
    persisted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "row_number": self.row_number,
            "status": self.status.value,
            "reason_codes": [code.value for code in self.reason_codes],
            "reasons": list(self.reasons),
            "fingerprint": self.fingerprint,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "oos_eligible": self.oos_eligible,
            "not_oos_eligible_reason": (
                self.not_oos_eligible_reason.value if self.not_oos_eligible_reason else None
            ),
            "persisted": self.persisted,
        }


@dataclass(frozen=True, slots=True)
class IntakeCounts:
    """批次计数（``accepted`` 只表示通过契约校验，不代表已落库）。"""

    rows: int = 0
    accepted: int = 0
    quarantined: int = 0
    duplicate: int = 0
    oos_eligible: int = 0
    not_oos_eligible: int = 0
    persisted: int = 0
    sources_created: int = 0
    processed_success: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "rows": self.rows,
            "accepted": self.accepted,
            "quarantined": self.quarantined,
            "duplicate": self.duplicate,
            "oos_eligible": self.oos_eligible,
            "not_oos_eligible": self.not_oos_eligible,
            "persisted": self.persisted,
            "sources_created": self.sources_created,
            "processed_success": self.processed_success,
        }


@dataclass(frozen=True, slots=True)
class EvidenceIntakeReport:
    """一次证据导入的完整报告（dry-run 与提交共用同一结构）。"""

    scope: EvidenceScope
    dry_run: bool
    generated_at: datetime
    input_file: InputFile
    rows: tuple[RowOutcome, ...]
    counts: IntakeCounts
    notes: tuple[str, ...] = ()
    schema_version: int = EVIDENCE_SCHEMA_VERSION
    contract_version: str = EVIDENCE_CONTRACT_VERSION

    @property
    def quarantined(self) -> tuple[RowOutcome, ...]:
        return tuple(row for row in self.rows if row.status is RowStatus.QUARANTINED)

    @property
    def accepted(self) -> tuple[RowOutcome, ...]:
        return tuple(row for row in self.rows if row.status is RowStatus.ACCEPTED)

    def to_dict(self) -> dict[str, Any]:
        """稳定机器可读结构（``--json`` 与 manifest 共用）。"""
        return {
            "schema_version": self.schema_version,
            "report": "授权证据 Evidence Intake 报告",
            "contract_version": self.contract_version,
            "scope": self.scope.value,
            "dry_run": self.dry_run,
            "generated_at": self.generated_at.isoformat(),
            "input": self.input_file.to_dict(),
            "counts": self.counts.to_dict(),
            "rows": [row.to_dict() for row in self.rows],
            "notes": list(self.notes),
        }

    def render(self) -> str:
        """人类可读 Markdown（与 JSON 同一事实来源）。"""
        from src.evidence.report import render_intake_report

        return render_intake_report(self)


def _unreadable_assessment(row: InputRow, *, index: int, scope: EvidenceScope) -> RowAssessment:
    """无法解析的行 → 隔离结论（原因文本不含行内容，避免把凭据带进报告）。"""
    return RowAssessment(
        index=index,
        row_number=row.row_number,
        scope=scope,
        source="",
        source_record_id="",
        fingerprint=None,
        reason_codes=(ReasonCode.ROW_UNREADABLE,),
        reasons=(row.error or "无法解析的行",),
        ignored_columns=(),
        record=None,
    )


def _existing_content_hashes(
    session: Session, identities: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """批量查询库内已存在的 ``(source, source_record_id) → content_hash``（**只读**）。"""
    names = sorted({source for source, _record in identities})
    record_ids = sorted({record for _source, record in identities})
    if not names or not record_ids:
        return {}
    rows = session.execute(
        sa.select(Source.name, RawItem.source_record_id, RawItem.content_hash)
        .join(RawItem, RawItem.source_id == Source.id)
        .where(Source.name.in_(names))
        .where(RawItem.source_record_id.in_(record_ids))
    ).all()
    return {
        (str(name), str(record_id)): str(content_hash)
        for name, record_id, content_hash in rows
    }


def _get_or_create_source(session: Session, name: str) -> tuple[Source, bool]:
    """按名称取来源；缺失时创建**禁用**来源（``enabled=false``、无采集器配置）。

    刻意不写 ``config_json``：本层不为任何来源注册采集器，
    因此新建来源不会被 Scheduler 采集；来源启用仍需人工 Gate
    （``config/rss_sources.json`` 的 ``enabled/verified`` 规则不变）。
    """
    existing = session.scalar(sa.select(Source).where(Source.name == name))
    if existing is not None:
        return existing, False
    source = Source(
        name=name,
        source_type=SourceType.NEWS,
        timezone="UTC",
        enabled=False,
        base_url=None,
        config_json=None,
    )
    session.add(source)
    session.flush()
    return source, True


def _raw_json(scope: EvidenceScope, record: EvidenceRecord) -> dict[str, Any]:
    """构造写入 ``raw_items.raw_json`` 的**白名单**载荷（不含输入里的额外列值）。"""
    evidence = record.to_evidence_json()
    return {
        "import_kind": "authorized_evidence_intake",
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "evidence_scope": scope.value,
        "evidence_oos_eligible": record.oos_eligible,
        "evidence_not_oos_eligible_reason": evidence["not_oos_eligible_reason"],
        "evidence_fingerprint": record.fingerprint,
        "evidence": evidence,
    }


def _persist_record(
    session: Session,
    *,
    scope: EvidenceScope,
    record: EvidenceRecord,
    source: Source,
    processor: CollectionProcessor,
) -> bool:
    """append-only 落库：先写 ``raw_items``，再经现有 Processor 写 ``processed_items``。

    Returns:
        Processor 是否产出新的加工事实（``SUCCESS``）。
    """
    raw = RawItem(
        source_id=source.id,
        source_record_id=record.source_record_id,
        item_type=RawItemType(scope.item_type),
        title=record.title,
        content_text=record.content,
        raw_json=_raw_json(scope, record),
        source_url=record.url,
        content_hash=record.content_hash,
        published_at=record.published_at,
        collected_at=record.collected_at,
        effective_at=record.effective_at,
    )
    session.add(raw)
    session.flush()
    processed = processor.process_persisted(session, raw)
    return bool(processed.accepted)


def _merge_codes(
    first: tuple[ReasonCode, ...], second: tuple[ReasonCode, ...]
) -> tuple[ReasonCode, ...]:
    """合并原因码（保序去重）。"""
    merged: list[ReasonCode] = []
    for code in (*first, *second):
        if code not in merged:
            merged.append(code)
    return tuple(merged)


def intake_evidence(
    session: Session,
    *,
    scope: EvidenceScope,
    input_file: InputFile,
    rows: Sequence[InputRow],
    moment: datetime,
    dry_run: bool = True,
) -> EvidenceIntakeReport:
    """执行一次证据导入（默认 dry-run：**零写入**）。

    Args:
        session: 数据库会话（dry-run 时只读）。
        scope: 命令声明的证据类别。
        input_file: 输入文件标识（路径 / 格式 / SHA-256）。
        rows: 逐行输入（``read_input_file`` 的结果）。
        moment: 审计时钟（必须带时区）；同时用于"未来时间"判定与 ``ingested_at``。
        dry_run: ``True`` 时只校验并报告；``False`` 时 append-only 提交。

    Raises:
        ValueError: ``moment`` 未带时区。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment 必须包含时区")
    moment = moment.astimezone(UTC)

    assessments: list[RowAssessment] = []
    for index, row in enumerate(rows):
        data = row.data
        if data is None:
            assessments.append(_unreadable_assessment(row, index=index, scope=scope))
            continue
        assessments.append(
            assess_row(data, scope=scope, index=index, row_number=row.row_number, moment=moment)
        )

    pending: list[tuple[RowAssessment, EvidenceRecord]] = [
        (assessment, record)
        for assessment in assessments
        if (record := assessment.record) is not None
    ]
    existing = _existing_content_hashes(
        session, [(record.source, record.source_record_id) for _a, record in pending]
    )

    statuses: dict[int, tuple[RowStatus, tuple[ReasonCode, ...], tuple[str, ...]]] = {}
    seen: dict[tuple[str, str], None] = {}
    for assessment, record in pending:
        identity = (record.source, record.source_record_id)
        if identity in seen:
            statuses[assessment.index] = (
                RowStatus.DUPLICATE,
                (ReasonCode.DUPLICATE,),
                ("同一批次内 (source, source_record_id) 重复：仅保留首条，幂等",),
            )
            continue
        seen[identity] = None
        stored_hash = existing.get(identity)
        if stored_hash is None:
            statuses[assessment.index] = (RowStatus.ACCEPTED, (), ())
        elif stored_hash == record.content_hash:
            statuses[assessment.index] = (
                RowStatus.DUPLICATE,
                (ReasonCode.DUPLICATE,),
                ("库内已存在同一 (source, source_record_id) 且内容一致：幂等重放，不重复落库",),
            )
        else:
            statuses[assessment.index] = (
                RowStatus.QUARANTINED,
                (ReasonCode.IDENTITY_CONFLICT,),
                (
                    "库内已有同一 (source, source_record_id) 但内容不同："
                    "拒绝覆盖历史事实，请改用新的 source_record_id 追加",
                ),
            )

    persisted_indexes: set[int] = set()
    sources_created = 0
    processed_success = 0
    if not dry_run:
        processor = CollectionProcessor(clock=lambda: moment)
        source_cache: dict[str, Source] = {}
        for assessment, record in pending:
            status = statuses[assessment.index][0]
            if status is not RowStatus.ACCEPTED:
                continue
            source = source_cache.get(record.source)
            if source is None:
                source, created = _get_or_create_source(session, record.source)
                source_cache[record.source] = source
                sources_created += int(created)
            if _persist_record(
                session, scope=scope, record=record, source=source, processor=processor
            ):
                processed_success += 1
            persisted_indexes.add(assessment.index)

    outcomes: list[RowOutcome] = []
    for assessment in assessments:
        status, extra_codes, extra_texts = statuses.get(
            assessment.index, (assessment.status, (), ())
        )
        outcomes.append(
            RowOutcome(
                index=assessment.index,
                row_number=assessment.row_number,
                status=status,
                reason_codes=_merge_codes(assessment.reason_codes, extra_codes),
                reasons=(*assessment.reasons, *extra_texts),
                fingerprint=assessment.fingerprint,
                source=assessment.source,
                source_record_id=assessment.source_record_id,
                oos_eligible=bool(status is RowStatus.ACCEPTED and assessment.oos_eligible),
                not_oos_eligible_reason=assessment.not_oos_eligible_reason,
                persisted=assessment.index in persisted_indexes,
            )
        )

    accepted = sum(1 for row in outcomes if row.status is RowStatus.ACCEPTED)
    quarantined = sum(1 for row in outcomes if row.status is RowStatus.QUARANTINED)
    duplicate = sum(1 for row in outcomes if row.status is RowStatus.DUPLICATE)
    oos_eligible = sum(
        1 for row in outcomes if row.status is RowStatus.ACCEPTED and row.oos_eligible
    )
    notes = [
        "本命令不联网：只读取本地输入文件，不抓取任何站点，不绕过 robots / 条款 / 证书限制。",
        "入库的 raw_items 不可覆盖：同一 (source, source_record_id) 内容变化判 "
        f"{ReasonCode.IDENTITY_CONFLICT.value} 并隔离。",
        "自动创建的 sources 行 enabled=false 且不写 config_json（不注册采集器）；"
        "来源启用与抓取仍需人工 Gate。",
        "只有具备独立历史可用证据（available_at + availability_provenance + "
        "availability_reference）的记录计入 OOS 证据；其余标记 "
        f"{ReasonCode.AVAILABILITY_UNPROVEN.value} → NOT_OOS_ELIGIBLE。",
    ]
    if dry_run:
        notes.append("dry-run：未写数据库、未创建 source、未落 manifest / quarantine 文件。")
    return EvidenceIntakeReport(
        scope=scope,
        dry_run=dry_run,
        generated_at=moment,
        input_file=input_file,
        rows=tuple(outcomes),
        counts=IntakeCounts(
            rows=len(outcomes),
            accepted=accepted,
            quarantined=quarantined,
            duplicate=duplicate,
            oos_eligible=oos_eligible,
            not_oos_eligible=accepted - oos_eligible,
            persisted=len(persisted_indexes),
            sources_created=sources_created,
            processed_success=processed_success,
        ),
        notes=tuple(notes),
    )





