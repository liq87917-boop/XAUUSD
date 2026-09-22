"""Author / News 证据输入模板（GOLD-006，operator-ready）。

本模块提供**可填写**的证据输入模板，解决\"业务方拿到契约却不知道怎么写文件\"的问题，
同时保证模板 / 示例**永远不可能**被计入真实可信证据：

1. 每行都带显式示例标记列（``record_kind=example``、``is_mock=true``）；
2. 其余字段一律填**非法占位值**（时间字段不是合法 ISO8601、``authorization_status=PENDING``、
   ``permits_* = false``）——即使有人手工删掉标记列，该行也会因授权缺失 / 时间非法被隔离；
3. 导入入口 (`scripts/intake_evidence.py`) 命中标记列即判
   :data:`ReasonCode.SYNTHETIC_EVIDENCE` 并整行隔离，**不写库、不计入 qualification ledger**。

模板生成**零网络、零写库**：只由本地字符串拼接产生，默认打印到 stdout。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Final

from src.evidence.contracts import (
    SYSTEM_ASSIGNED_FIELDS,
    EvidenceScope,
    canonical_field_names,
)

__all__ = [
    "EXAMPLE_MARKER_COLUMNS",
    "TEMPLATE_FORMATS",
    "TEMPLATE_ROOT",
    "TEMPLATE_SCHEMA_VERSION",
    "example_rows",
    "render_template",
    "template_columns",
    "template_output_name",
    "write_template",
]

#: 模板 schema 版本（列增删必须同步升版本 + 更新测试与 README）
TEMPLATE_SCHEMA_VERSION: Final[int] = 1
#: 支持的模板格式（标准库即可写出，不新增依赖）
TEMPLATE_FORMATS: Final[tuple[str, ...]] = ("csv", "jsonl")
#: 示例 / 合成标记列（写在模板每一行上；导入时被判 ``SYNTHETIC_EVIDENCE`` 隔离）。
#: 必须同时出现在 ``EXAMPLE_MARKER_KIND_FIELDS`` / ``EXAMPLE_MARKER_FLAG_FIELDS`` 里，
#: 否则模板示例就会被当成真实证据（由 ``tests/unit/test_evidence_templates.py`` 锁定）。
EXAMPLE_MARKER_COLUMNS: Final[tuple[str, ...]] = ("record_kind", "is_mock")
#: 模板默认输出目录（仓库内相对路径；仅本地文件，不联网）
TEMPLATE_ROOT: Final[Path] = Path("examples") / "evidence"

#: 时间类字段的非法占位值（故意不是合法 ISO8601：即使标记列被删也过不了时间校验）
_TIME_PLACEHOLDER: Final[str] = "FILL_ME_ISO8601_必须带时区"


def template_columns(scope: EvidenceScope) -> tuple[str, ...]:
    """模板列（契约字段去掉系统赋值列，再追加示例标记列）。"""
    columns = [
        name for name in canonical_field_names() if name not in SYSTEM_ASSIGNED_FIELDS
    ]
    return tuple(columns) + EXAMPLE_MARKER_COLUMNS


def _placeholder(name: str) -> str:
    return f"FILL_ME_{name}"


def _example_row(scope: EvidenceScope, index: int) -> dict[str, str]:
    """构造一条**明确标记为示例且填非法占位值**的模板行。"""
    row = {name: _placeholder(name) for name in template_columns(scope)}
    row.update(
        {
            "source": f"FILL_ME_source_{(index)}",
            "source_record_id": f"FILL_ME_record_id_{index}",
            "author_name": ("FILL_ME_作者显示名" if scope is EvidenceScope.AUTHOR else ""),
            "external_account_id": (
                "FILL_ME_平台账号ID" if scope is EvidenceScope.AUTHOR else ""
            ),
            "content": "FILL_ME_原文（不改写、不总结）",
            "content_ref": "",
            "published_at": _TIME_PLACEHOLDER,
            "collected_at": _TIME_PLACEHOLDER,
            "available_at": _TIME_PLACEHOLDER,
            "availability_provenance": "FILL_ME_证据形式",
            "availability_reference": "FILL_ME_https_引用或 docs/legal 路径",
            "provenance_reference": "FILL_ME_https_出处引用",
            "url": "FILL_ME_https_原始页面",
            "language": "zh",
            # 授权：示例行一律 PENDING / false —— 即使标记列被删也绝不放行
            "authorization_status": "PENDING",
            "authorization_basis": "FILL_ME_官方API_或_许可协议_或_书面许可_或_自有账号",
            "authorization_reference": "FILL_ME_https_条款或 docs/legal 路径",
            "authorization_reviewed_by": "FILL_ME_实际核验授权的人",
            "authorization_reviewed_at": _TIME_PLACEHOLDER,
            "authorization_valid_from": _TIME_PLACEHOLDER,
            "authorization_expires_at": _TIME_PLACEHOLDER,
            "permits_automated_collection": "false",
            "permits_local_storage": "false",
            "permits_research_use": "false",
            # 示例标记：导入时判 SYNTHETIC_EVIDENCE 隔离
            "record_kind": "example",
            "is_mock": "true",
        }
    )
    return row


def example_rows(scope: EvidenceScope) -> tuple[dict[str, str], ...]:
    """返回该 scope 的示例行（默认两条，便于 operator 看清\"一记录一行\"）。"""
    return (_example_row(scope, 1), _example_row(scope, 2))


def render_template(scope: EvidenceScope, fmt: str = "csv") -> str:
    """渲染模板文本（``csv`` 或 ``jsonl``）；**纯本地字符串拼接，零网络、零写库**。"""
    name = fmt.strip().lower()
    if name not in TEMPLATE_FORMATS:
        raise ValueError(f"不支持的模板格式：{fmt!r}；可选 {TEMPLATE_FORMATS}")
    columns = template_columns(scope)
    rows = example_rows(scope)
    if name == "jsonl":
        return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def template_output_name(scope: EvidenceScope, fmt: str = "csv") -> str:
    """模板默认文件名（按 scope 与格式）。"""
    name = fmt.strip().lower()
    if name not in TEMPLATE_FORMATS:
        raise ValueError(f"不支持的模板格式：{fmt!r}；可选 {TEMPLATE_FORMATS}")
    return f"{scope.value}_evidence_template.{name}"


def write_template(
    scope: EvidenceScope,
    fmt: str,
    target: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """把模板写到本地文件（UTF-8，无 BOM）。

    Raises:
        FileExistsError: 目标已存在且未显式 ``overwrite=True``（防止覆盖 operator 已填写的内容）。
    """
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"模板目标已存在：{target}；确认不会覆盖已填写内容后再用 --overwrite"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_template(scope, fmt), encoding="utf-8")
    return target
