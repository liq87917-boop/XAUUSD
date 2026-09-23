"""GOLD-017 Evidence Package / Manifest Builder 单元测试（临时目录 / 零数据库 / 零网络）。

覆盖：
- 确定性摘要与排序：``manifest.files`` 按规范化相对路径排序、SHA-256 来自**真实字节**、
  同输入（含声明顺序不同）→ **byte-stable**；
- manifest 与 GOLD-011 契约一致：键 / 必填项 / 文件条目键 / 格式词表，且**没有任何时间字段**；
- 显式元数据缺失 / 非法：fail-closed + 稳定原因码（绝不推断授权 / 时间 / availability / OOS）；
- 路径与文件安全：绝对路径 / ``..`` / 多级路径 / 符号链接 / 目录 / ``manifest.json`` 自引用 /
  未支持扩展 / 重复 / 大小写冲突 / 包内未声明文件 / 包目录符号链接；
- 示例 / 合成名称、敏感值（凭据键名 / 疑似 blob / 可被脱敏规则命中的取值）一律拒绝；
- 写入前同源预检：accepted / quarantined / not_oos_eligible 与稳定原因码；不改写 evidence 原文；
- dry-run **零写入**、原子写无残留、幂等同内容、不同内容冲突、锁冲突零写入；
- 脱敏（凭据绝不入 artifact / 错误信息）与源码守卫（无网络 / 无数据库 / 不删移文件 / 无 mtime）。
"""

from __future__ import annotations

import ast
import csv
import io
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.common import hashing
from src.evidence import (
    EVIDENCE_CONTRACT_VERSION,
    EXIT_CONFIG_ERROR,
    EXIT_INPUT_INVALID,
    EXIT_LOCK_CONFLICT,
    EXIT_MANIFEST_CONFLICT,
    EXIT_OK,
    EXIT_UNUSABLE,
    FILE_ENTRY_REQUIRED_FIELDS,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    MANIFEST_REQUIRED_FIELDS,
    SUPPORTED_FILE_FORMATS,
    EvidencePackageArgumentError,
    EvidencePackageCode,
    EvidencePackageConflictError,
    EvidencePackageInputError,
    EvidencePackagePathError,
    LockConflictError,
    ManifestWriteStatus,
    SingleInstanceLock,
    package_builder_exit_code_for,
    render_package_summary,
    run_package_builder,
)
from src.evidence import package_builder as builder
from src.evidence.inbox import ALLOWED_FILE_ENTRY_KEYS, ALLOWED_MANIFEST_KEYS
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
SECRET = "sk-livesecret0123456789"
EVIDENCE_NAME = "author-2026-06.jsonl"
FORBIDDEN_TIME_KEYS = (
    "published_at",
    "collected_at",
    "effective_at",
    "available_at",
    "ingested_at",
    "generated_at",
    "mtime",
    "ctime",
)


def author_row(record_id: str = "a-0001", **overrides: Any) -> dict[str, Any]:
    """一条**完全合规**的 Author 证据行（时间自洽、带时区、OOS 可用）。"""
    row: dict[str, Any] = {
        "source": "vendor-author",
        "source_record_id": record_id,
        "author_name": "张三",
        "external_account_id": "acct-0001",
        "content": "黄金短线看多",
        "published_at": "2026-06-01T00:00:00+00:00",
        "collected_at": "2026-06-01T01:00:00+00:00",
        "available_at": "2026-06-01T00:30:00+00:00",
        "availability_provenance": "provider_archive_export",
        "availability_reference": "https://vendor.example/archive/2026-06-01",
        "provenance_reference": "https://vendor.example/export/2026-06",
        "url": "https://vendor.example/posts/a-0001",
        "language": "zh",
        "authorization_status": "APPROVED",
        "authorization_basis": "written_permission",
        "authorization_reference": "https://vendor.example/terms",
        "authorization_reviewed_by": "operator-li",
        "authorization_reviewed_at": "2026-06-02T00:00:00+00:00",
        "permits_automated_collection": "true",
        "permits_local_storage": "true",
        "permits_research_use": "true",
    }
    row.update(overrides)
    return row


def news_row(record_id: str = "n-0001", **overrides: Any) -> dict[str, Any]:
    """一条**完全合规**的 News 证据行（不含作者身份字段）。"""
    row = author_row(record_id, **overrides)
    row.pop("author_name", None)
    row.pop("external_account_id", None)
    return row


def write_rows(path: Path, rows: Sequence[Mapping[str, Any]], *, fmt: str) -> None:
    """把若干行写成 ``jsonl`` 或 ``csv``（UTF-8；CSV 表头取自首行键集合）。"""
    if fmt == "jsonl":
        path.write_text(
            "".join(json.dumps(dict(row), ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))
    path.write_text(buffer.getvalue(), encoding="utf-8")


def metadata(**overrides: Any) -> dict[str, Any]:
    """**人工显式**元数据（测试里永远显式给出；工具绝不推断）。"""
    payload: dict[str, Any] = {
        "evidence_type": "author",
        "source": "vendor-author",
        "authorization_reference": "https://vendor.example/terms",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
    }
    payload.update(overrides)
    return payload


def make_package(
    tmp_path: Path,
    name: str = "pkg-author-01",
    *,
    fmt: str = "jsonl",
    evidence_name: str = EVIDENCE_NAME,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """在临时目录下建一个**只有 evidence 文件**的 package 目录（无 manifest）。"""
    package = tmp_path / name
    package.mkdir(parents=True, exist_ok=True)
    write_rows(package / evidence_name, list(rows if rows is not None else [author_row()]), fmt=fmt)
    return package


def build(package_dir: Path, *, files: Sequence[str] | None = None, **overrides: Any) -> Any:
    """用默认显式元数据调用 builder（可用 ``overrides`` 覆盖任意参数 / 元数据）。"""
    params = metadata()
    for key in list(params):
        if key in overrides:
            params[key] = overrides.pop(key)
    return run_package_builder(
        package_dir,
        files=tuple(files if files is not None else [EVIDENCE_NAME]),
        moment=overrides.pop("moment", MOMENT),
        **params,
        **overrides,
    )


def package_entries(package_dir: Path) -> dict[str, bytes]:
    """package 目录内**全部**条目的原始字节（用于断言“零写入 / 零改写”）。"""
    return {
        item.name: (item.read_bytes() if item.is_file() else b"")
        for item in sorted(package_dir.iterdir(), key=lambda entry: entry.name)
    }


def assert_no_time_keys(payload: Any) -> None:
    """递归断言：manifest 里**没有任何**时间 / 证据时间键。"""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            assert str(key) not in FORBIDDEN_TIME_KEYS, f"manifest 出现时间键：{key}"
            assert_no_time_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            assert_no_time_keys(item)


def test_exit_code_mapping_is_stable() -> None:
    assert EXIT_OK == 0 and EXIT_CONFIG_ERROR == 2 and EXIT_UNUSABLE == 3
    assert EXIT_INPUT_INVALID == 4 and EXIT_MANIFEST_CONFLICT == 5 and EXIT_LOCK_CONFLICT == 6
    assert package_builder_exit_code_for(EvidencePackageArgumentError("x")) == EXIT_CONFIG_ERROR
    assert package_builder_exit_code_for(EvidencePackagePathError("x")) == EXIT_UNUSABLE
    assert package_builder_exit_code_for(EvidencePackageInputError(["A"])) == EXIT_INPUT_INVALID
    assert (
        package_builder_exit_code_for(EvidencePackageConflictError("x")) == EXIT_MANIFEST_CONFLICT
    )
    assert package_builder_exit_code_for(LockConflictError("x")) == EXIT_LOCK_CONFLICT
    assert package_builder_exit_code_for(RuntimeError("未知")) == EXIT_INPUT_INVALID


def test_dry_run_preview_is_deterministic_and_writes_nothing(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    before = package_entries(package)
    preview = build(package)
    assert preview.write_status is ManifestWriteStatus.DRY_RUN
    assert preview.written_path is None
    assert preview.manifest_sha256 == hashing.sha256_bytes(builder.manifest_bytes(preview.manifest))
    assert package_entries(package) == before  # dry-run 零写入、零改写
    assert not (package / MANIFEST_FILE_NAME).exists()
    assert not (tmp_path / f"{package.name}.manifest.lock").exists()
    payload = preview.to_dict()
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["requires_human_action"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["auto_intake_allowed"] is False
    assert payload["writes_database"] is False
    assert payload["preflight"]["accepted"] == 1
    assert payload["preflight"]["outcome"] == builder.PreflightOutcome.ACCEPTED_WITH_OOS.value


def test_same_input_gives_byte_stable_manifest(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    write_rows(package / "b-extra.jsonl", [author_row("a-0002")], fmt="jsonl")
    first = build(package, files=[EVIDENCE_NAME, "b-extra.jsonl"])
    second = build(package, files=["b-extra.jsonl", EVIDENCE_NAME])  # 声明顺序不同
    assert builder.manifest_bytes(first.manifest) == builder.manifest_bytes(second.manifest)
    assert first.manifest_sha256 == second.manifest_sha256
    assert [item["path"] for item in first.manifest["files"]] == sorted(
        [EVIDENCE_NAME, "b-extra.jsonl"]
    )


def test_manifest_matches_gold011_contract_and_has_no_time_fields(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    preview = build(package)
    manifest = preview.manifest
    assert set(manifest) <= set(ALLOWED_MANIFEST_KEYS)
    for field in MANIFEST_REQUIRED_FIELDS:
        assert manifest[field]
    assert manifest["schema_version"] == INBOX_SCHEMA_VERSION
    assert manifest["contract_version"] == EVIDENCE_CONTRACT_VERSION
    assert manifest["historical_oos_applicable"] is True
    for entry in manifest["files"]:
        assert set(entry) <= set(ALLOWED_FILE_ENTRY_KEYS)
        for field in FILE_ENTRY_REQUIRED_FIELDS:
            assert entry[field]
        assert entry["format"] in SUPPORTED_FILE_FORMATS
        raw = (package / entry["path"]).read_bytes()
        assert entry["sha256"] == hashing.sha256_bytes(raw)  # 摘要来自**真实字节**
        assert len(entry["sha256"]) == 64 and entry["sha256"].islower()
    assert_no_time_keys(manifest)


@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
def test_csv_and_jsonl_formats_are_resolved(tmp_path: Path, fmt: str) -> None:
    name = "author.csv" if fmt == "csv" else EVIDENCE_NAME
    package = make_package(tmp_path, fmt=fmt, evidence_name=name)
    preview = build(package, files=[name])
    assert preview.manifest["files"][0]["format"] == fmt
    assert preview.preflight.accepted == 1


def test_json_suffix_resolves_to_jsonl_vocabulary(tmp_path: Path) -> None:
    package = make_package(tmp_path, evidence_name="author.json")
    preview = build(package, files=["author.json"])
    assert preview.manifest["files"][0]["format"] == "jsonl"


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("evidence_type", EvidencePackageCode.MANIFEST_FIELD_MISSING.value),
        ("source", EvidencePackageCode.METADATA_VALUE_INVALID.value),
        ("authorization_reference", EvidencePackageCode.MANIFEST_FIELD_MISSING.value),
        ("time_semantics", EvidencePackageCode.TIME_SEMANTICS_MISSING.value),
        ("availability_semantics", EvidencePackageCode.AVAILABILITY_SEMANTICS_MISSING.value),
        ("historical_oos_applicable", EvidencePackageCode.MANIFEST_FIELD_MISSING.value),
    ],
)
def test_missing_explicit_metadata_is_fail_closed(tmp_path: Path, field: str, code: str) -> None:
    package = make_package(tmp_path)
    before = package_entries(package)
    with pytest.raises(EvidencePackageArgumentError) as excinfo:
        build(package, **{field: None})
    assert code in str(excinfo.value)
    assert not (package / MANIFEST_FILE_NAME).exists()  # 绝不落盘
    assert package_entries(package) == before


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("evidence_type", "posts", EvidencePackageCode.EVIDENCE_TYPE_UNSUPPORTED.value),
        ("evidence_type", "Author", EvidencePackageCode.EVIDENCE_TYPE_UNSUPPORTED.value),
        ("source", "", EvidencePackageCode.METADATA_VALUE_INVALID.value),
        ("source", "vendor/sub", EvidencePackageCode.METADATA_VALUE_INVALID.value),
        ("source", "api_key", EvidencePackageCode.SENSITIVE_VALUE_DETECTED.value),
        ("time_semantics", "", EvidencePackageCode.TIME_SEMANTICS_MISSING.value),
        ("availability_semantics", "", EvidencePackageCode.AVAILABILITY_SEMANTICS_MISSING.value),
        (
            "authorization_reference",
            "http://vendor.example/terms",
            EvidencePackageCode.AUTHORIZATION_REFERENCE_INVALID.value,
        ),
        (
            "authorization_reference",
            "docs/legal/",
            EvidencePackageCode.AUTHORIZATION_REFERENCE_INVALID.value,
        ),
        ("historical_oos_applicable", "true", EvidencePackageCode.MANIFEST_FIELD_MISSING.value),
    ],
)
def test_invalid_metadata_values_are_fail_closed(
    tmp_path: Path, field: str, value: Any, code: str
) -> None:
    package = make_package(tmp_path)
    with pytest.raises(EvidencePackageArgumentError) as excinfo:
        build(package, **{field: value})
    assert code in str(excinfo.value)
    assert not (package / MANIFEST_FILE_NAME).exists()


def test_empty_file_list_is_fail_closed(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    with pytest.raises(EvidencePackageArgumentError) as excinfo:
        build(package, files=[])
    assert EvidencePackageCode.NO_INPUT_FILES.value in str(excinfo.value)


def test_naive_moment_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    with pytest.raises(EvidencePackageArgumentError):
        build(package, moment=datetime(2026, 9, 23, 0, 0))


def test_missing_package_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(EvidencePackagePathError):
        build(tmp_path / "nope")


def test_symlinked_package_dir_is_rejected(tmp_path: Path) -> None:
    make_package(tmp_path)
    link = tmp_path / "linked-package"
    try:
        link.symlink_to(tmp_path / "pkg-author-01", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows 权限
        pytest.skip(f"本环境不允许创建符号链接：{exc}")
    with pytest.raises(EvidencePackagePathError):
        build(link)


@pytest.mark.parametrize(
    ("declared", "code"),
    [
        ("../outside.jsonl", EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value),
        ("sub/nested.jsonl", EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value),
        (MANIFEST_FILE_NAME, EvidencePackageCode.MANIFEST_SELF_REFERENCE.value),
        ("author.txt", EvidencePackageCode.UNSUPPORTED_FILE_FORMAT.value),
        ("missing.jsonl", EvidencePackageCode.EVIDENCE_FILE_MISSING.value),
    ],
)
def test_unsafe_or_missing_declared_files_are_rejected(
    tmp_path: Path, declared: str, code: str
) -> None:
    package = make_package(tmp_path)
    if declared == "author.txt":
        (package / declared).write_text("纯文本不是受支持格式\n", encoding="utf-8")
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package, files=[declared])
    assert code in excinfo.value.codes
    assert not (package / MANIFEST_FILE_NAME).exists()


def test_duplicate_declared_file_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package, files=[EVIDENCE_NAME, EVIDENCE_NAME])
    assert EvidencePackageCode.DUPLICATE_FILE_PATH.value in excinfo.value.codes


def test_absolute_path_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package, files=[str(package / EVIDENCE_NAME)])
    assert EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value in excinfo.value.codes


def test_case_conflicting_declared_files_are_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    upper = package / EVIDENCE_NAME.upper()
    upper.write_bytes((package / EVIDENCE_NAME).read_bytes())
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package, files=[EVIDENCE_NAME, EVIDENCE_NAME.upper()])
    assert EvidencePackageCode.FILE_NAME_CASE_CONFLICT.value in excinfo.value.codes


def test_directory_inside_package_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    (package / "sub").mkdir()
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package, files=["sub"])
    assert EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value in excinfo.value.codes


def test_undeclared_file_inside_package_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    (package / "leftover.txt").write_text("未声明", encoding="utf-8")
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package)
    assert EvidencePackageCode.EVIDENCE_FILE_UNDECLARED.value in excinfo.value.codes


def test_symlinked_evidence_file_is_rejected_without_following(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    real = tmp_path / "real-author.jsonl"
    write_rows(real, [author_row()], fmt="jsonl")
    link = package / "linked-author.jsonl"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows 权限
        pytest.skip(f"本环境不允许创建符号链接：{exc}")
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package, files=["linked-author.jsonl"])
    assert EvidencePackageCode.EVIDENCE_FILE_PATH_UNSAFE.value in excinfo.value.codes


def test_synthetic_package_name_is_rejected(tmp_path: Path) -> None:
    package = make_package(tmp_path, name="author-sample-01")
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package)
    assert EvidencePackageCode.SYNTHETIC_EVIDENCE.value in excinfo.value.codes


def test_synthetic_file_name_is_rejected(tmp_path: Path) -> None:
    name = "author-example.jsonl"
    package = make_package(tmp_path, evidence_name=name)
    with pytest.raises(EvidencePackageInputError) as excinfo:
        build(package, files=[name])
    assert EvidencePackageCode.SYNTHETIC_EVIDENCE.value in excinfo.value.codes


def test_credential_like_reference_is_rejected_without_leaking(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    reference = f"https://vendor.example/terms?api_key={SECRET}"
    with pytest.raises(EvidencePackageArgumentError) as excinfo:
        build(package, authorization_reference=reference)
    message = str(excinfo.value)
    assert EvidencePackageCode.SENSITIVE_VALUE_DETECTED.value in message
    assert SECRET not in message  # 凭据绝不回显
    assert not (package / MANIFEST_FILE_NAME).exists()


@pytest.mark.parametrize("notes", ["token=abc123456", "A" * 60, "Authorization: Bearer abc"])
def test_credential_like_notes_are_rejected(tmp_path: Path, notes: str) -> None:
    package = make_package(tmp_path)
    with pytest.raises(EvidencePackageArgumentError) as excinfo:
        build(package, notes=notes)
    assert EvidencePackageCode.SENSITIVE_VALUE_DETECTED.value in str(excinfo.value)


def test_notes_are_stored_redacted_and_capped(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    preview = build(package, notes="供应商授权档案 2026-06（人工备注）")
    assert preview.manifest["notes"] == "供应商授权档案 2026-06（人工备注）"
    long_preview = build(package, notes="人工备注 " * 250)
    assert len(long_preview.manifest["notes"]) == builder.MAX_NOTES_CHARS
    with pytest.raises(EvidencePackageArgumentError) as excinfo:
        build(package, notes="n" * (builder.MAX_NOTES_INPUT_CHARS + 1))
    assert EvidencePackageCode.NOTES_TOO_LONG.value in str(excinfo.value)


def test_preflight_reports_quarantine_and_not_oos_without_leaking_content(tmp_path: Path) -> None:
    rows = [
        author_row("a-0001"),
        author_row("a-0002", authorization_status="PENDING"),
        author_row(
            "a-0003",
            available_at="",
            availability_provenance="",
            availability_reference="",
        ),
    ]
    package = make_package(tmp_path, rows=rows)
    before = package_entries(package)
    preview = build(package)
    assert preview.preflight.rows == 3
    assert preview.preflight.accepted == 2
    assert preview.preflight.quarantined == 1
    assert preview.preflight.not_oos_eligible == 1
    assert preview.preflight.outcome is builder.PreflightOutcome.HAS_QUARANTINED_ROWS
    counts = dict(preview.preflight.code_counts)
    assert counts["AUTHORIZATION_MISSING"] == 1
    assert counts["AVAILABILITY_UNPROVEN"] == 1
    assert preview.preflight.per_file == ((EVIDENCE_NAME, 3, 2, 1, 1),)
    blob = json.dumps(preview.to_dict(), ensure_ascii=False)
    assert "黄金短线看多" not in blob  # 绝不带出正文
    assert "a-0003" not in blob  # 绝不带出记录 ID
    assert package_entries(package) == before  # 预检不改写任何原始行
    assert any("CONTAINS_QUARANTINED_ROWS" in item for item in preview.warnings)
    assert any("AVAILABILITY_UNPROVEN" in item for item in preview.warnings)


def test_unreadable_row_is_counted_as_quarantined(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    (package / EVIDENCE_NAME).write_text(
        json.dumps(author_row(), ensure_ascii=False) + "\n{ not json\n",
        encoding="utf-8",
    )
    preview = build(package)
    assert preview.preflight.quarantined == 1
    assert dict(preview.preflight.code_counts)["ROW_UNREADABLE"] == 1


def test_declared_oos_false_is_warned_but_not_inferred(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    preview = build(package, historical_oos_applicable=False)
    assert preview.manifest["historical_oos_applicable"] is False
    assert any("AVAILABILITY_UNPROVEN" in item for item in preview.warnings)


def test_out_path_must_be_exact_manifest_of_package(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "manifest.json"
    with pytest.raises(EvidencePackagePathError) as excinfo:
        build(package, out_path=elsewhere)
    assert EvidencePackageCode.MANIFEST_OUT_NOT_TARGET.value in str(excinfo.value)
    assert not elsewhere.exists()
    with pytest.raises(EvidencePackagePathError):
        build(package, out_path=package)
    assert not (package / MANIFEST_FILE_NAME).exists()
    assert package_entries(package) == {EVIDENCE_NAME: (package / EVIDENCE_NAME).read_bytes()}


def test_write_is_atomic_without_residue_and_outside_lock(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    preview = build(package, out_path=package / MANIFEST_FILE_NAME)
    assert preview.write_status is ManifestWriteStatus.WRITTEN
    assert preview.written_path is not None
    manifest_file = package / MANIFEST_FILE_NAME
    assert manifest_file.read_bytes() == builder.manifest_bytes(preview.manifest)
    assert json.loads(manifest_file.read_text(encoding="utf-8")) == preview.manifest
    assert not list(package.glob("*.tmp")) and not list(package.glob(".*.tmp"))
    assert sorted(item.name for item in package.iterdir()) == [EVIDENCE_NAME, MANIFEST_FILE_NAME]
    # 锁文件**刻意放在 package 目录之外**（否则会成为包内未声明文件）
    assert (tmp_path / f"{package.name}.manifest.lock").exists()


def test_second_write_with_same_content_is_idempotent(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    first = build(package, out_path=package / MANIFEST_FILE_NAME)
    manifest_file = package / MANIFEST_FILE_NAME
    original = manifest_file.read_bytes()
    second = build(package, out_path=package / MANIFEST_FILE_NAME)
    assert second.write_status is ManifestWriteStatus.IDEMPOTENT_UNCHANGED
    assert manifest_file.read_bytes() == original == builder.manifest_bytes(first.manifest)


def test_different_content_never_silently_overwrites_manifest(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    build(package, out_path=package / MANIFEST_FILE_NAME)
    manifest_file = package / MANIFEST_FILE_NAME
    original = manifest_file.read_bytes()
    write_rows(package / EVIDENCE_NAME, [author_row("a-0001"), author_row("a-0002")], fmt="jsonl")
    with pytest.raises(EvidencePackageConflictError) as excinfo:
        build(package, out_path=package / MANIFEST_FILE_NAME)
    assert EvidencePackageCode.MANIFEST_CONFLICT.value in str(excinfo.value)
    assert manifest_file.read_bytes() == original  # 旧 manifest 原样保留


def test_existing_corrupt_manifest_is_never_overwritten(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    manifest_file = package / MANIFEST_FILE_NAME
    manifest_file.write_text("{ not json", encoding="utf-8")
    with pytest.raises(EvidencePackageConflictError):
        build(package, out_path=manifest_file)
    assert manifest_file.read_text(encoding="utf-8") == "{ not json"


def test_lock_conflict_is_fail_closed_without_writing(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    lock_path = tmp_path / f"{package.name}.manifest.lock"
    with SingleInstanceLock(lock_path, owner="holder"):
        with pytest.raises(LockConflictError):
            build(package, out_path=package / MANIFEST_FILE_NAME)
        assert not (package / MANIFEST_FILE_NAME).exists()  # 零写入
        assert lock_path.exists()  # 活动锁不被删除


def test_news_scope_package_is_supported(tmp_path: Path) -> None:
    name = "news-2026-06.jsonl"
    package = make_package(tmp_path, name="pkg-news-01", evidence_name=name, rows=[news_row()])
    preview = build(package, files=[name], evidence_type="news", source="vendor-news")
    assert preview.manifest["evidence_type"] == "news"
    assert preview.preflight.accepted == 1


def test_render_summary_keeps_blocker_and_is_redacted(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    preview = build(package)
    summary = render_package_summary(preview)
    assert PHASE3_3_BLOCKER_CODE in summary
    assert "blocker_active=true" in summary
    assert "human_gate_required=true" in summary
    assert "data_qualification_passed=false" in summary
    assert "不自动 intake" in summary
    assert MANIFEST_FILE_NAME in summary
    assert EVIDENCE_NAME in summary
    assert SECRET not in summary


def test_module_has_no_network_database_or_destructive_behaviour() -> None:
    source = Path(builder.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    for banned in ("aiohttp", "httpx", "requests", "socket", "urllib", "sqlalchemy", "subprocess"):
        assert all(banned not in module for module in modules), f"不得引入 {banned}：{modules}"
    # 绝不删除 / 移动 / 改名原始 evidence；绝不调用任何 intake / commit 路径
    for banned_call in ("unlink", "rmtree", "os.remove", "os.rename", "shutil.move"):
        assert banned_call not in source
    assert "intake_evidence" not in source
    assert "atomic_write_text" in source  # 唯一写路径 = 既有原子写原语
    # 绝不读取 / 写入文件时间，也绝不使用“当前时间”充当证据时间（只用注入的审计时点）
    banned_times = ("getmtime", "getctime", "st_mtime", "st_ctime", "datetime.now", "time.time")
    for banned_time in banned_times:
        assert banned_time not in source
    assert "PROJECT_STATE.json" not in source
    # 无网络 / 无数据库 / 无常驻循环
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
