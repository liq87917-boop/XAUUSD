"""GOLD-011 Evidence Inbox 本地发现与预检单元测试（临时目录 / 零数据库 / 零网络）。

覆盖：
- 空 inbox：0 候选、无待处理项、安全字段恒定；
- 合法候选（JSONL / CSV）：manifest 齐全 + 内容 SHA-256 核对通过 → ``PREFLIGHT_PASS``；
- 缺 manifest / manifest 损坏 / schema 不符 / 缺必填声明 / 类型非法：fail-closed + 稳定原因码；
- 摘要不一致 / 未声明文件 / 路径穿越 / 绝对路径 / 符号链接：fail-closed 且不越界读取；
- 模板 / 示例 / Mock：``SYNTHETIC_EVIDENCE`` 整包隔离，永不计入资格；
- 行级隔离 / 无可用行 / 缺 OOS 证据：沿用 gateway 原因码并整包隔离；
- 重复扫描幂等（同内容不重复生成待处理项、``first_seen_at`` 保留）；
- 内容变更 → 新指纹 → 新待处理项；指纹不受 mtime / 扫描时间影响；
- pending state 原子写、损坏 state fail-closed、锁冲突零写入、退出码稳定；
- 全量脱敏（凭据 / URL 查询串）与源码守卫（无网络 / 无数据库 / 不删移原始文件）。
"""

from __future__ import annotations

import ast
import csv
import io
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.common import hashing
from src.evidence import (
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_CONFLICT,
    EXIT_NO_CANDIDATES,
    EXIT_OK,
    EXIT_STATE_INVALID,
    EXIT_UNUSABLE,
    INBOX_SCHEMA_VERSION,
    MANIFEST_FILE_NAME,
    PENDING_KIND,
    InboxDirError,
    InboxReasonCode,
    InboxStatus,
    LockConflictError,
    LockUnavailableError,
    PendingStateError,
    SingleInstanceLock,
    inbox_exit_code_for,
    load_pending_register,
    render_inbox_summary,
    run_inbox_scan,
    scan_inbox,
)
from src.evidence import inbox as inbox_module
from src.monitoring import PHASE3_3_BLOCKER_CODE

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
LATER = MOMENT.replace(hour=6)
SECRET = "sk-livesecret0123456789"
EVIDENCE_NAME = "author.jsonl"


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


def manifest_payload(
    evidence_path: Path, *, evidence_name: str = EVIDENCE_NAME, **overrides: Any
) -> dict[str, Any]:
    """构造最小合规 manifest（摘要取自**实际文件内容**）。"""
    payload: dict[str, Any] = {
        "schema_version": INBOX_SCHEMA_VERSION,
        "contract_version": "evidence-intake-v1",
        "evidence_type": "author",
        "source": "vendor-author",
        "authorization_reference": "https://vendor.example/terms",
        "time_semantics": "provider_export_iso8601_with_tz",
        "availability_semantics": "provider_archive_export_daily_snapshot",
        "historical_oos_applicable": True,
        "files": [
            {
                "path": evidence_name,
                "sha256": hashing.sha256_bytes(evidence_path.read_bytes()),
                "format": "jsonl" if evidence_name.endswith(".jsonl") else "csv",
            }
        ],
    }
    payload.update(overrides)
    return payload


def build_package(
    root: Path,
    name: str = "pkg-author-01",
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    fmt: str = "jsonl",
    evidence_name: str = EVIDENCE_NAME,
    manifest: Mapping[str, Any] | str | None = None,
    manifest_name: str = MANIFEST_FILE_NAME,
) -> Path:
    """在 ``root`` 下建一个候选包目录（默认合规；可用 ``manifest`` 覆盖 / 破坏）。"""
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    payload_rows = list(rows if rows is not None else [author_row()])
    evidence = package / evidence_name
    write_rows(evidence, payload_rows, fmt=fmt)
    if manifest is None:
        document = manifest_payload(evidence, evidence_name=evidence_name)
    elif isinstance(manifest, str):
        (package / manifest_name).write_text(manifest, encoding="utf-8")
        return package
    else:
        document = dict(manifest)
    (package / manifest_name).write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return package


def only_package(report: Any) -> Any:
    """取报告中唯一的候选包（测试断言用）。"""
    assert len(report.packages) == 1, report.to_dict()
    return report.packages[0]


def status_of(report: Any) -> dict[str, Any]:
    """取报告 JSON 的稳定结构（断言脱敏与安全字段用）。"""
    return report.to_dict()


def test_exit_code_mapping_is_stable() -> None:
    assert inbox_exit_code_for(PendingStateError("x")) == EXIT_STATE_INVALID
    assert inbox_exit_code_for(InboxDirError("x")) == EXIT_UNUSABLE
    assert inbox_exit_code_for(LockConflictError("x")) == EXIT_LOCK_CONFLICT
    assert inbox_exit_code_for(LockUnavailableError("x")) == EXIT_UNUSABLE
    assert inbox_exit_code_for(RuntimeError("未知")) == EXIT_STATE_INVALID  # 未知 → fail-closed
    assert EXIT_OK == 0 and EXIT_CONFIG_ERROR == 2 and EXIT_NO_CANDIDATES == 5


def test_empty_inbox_yields_no_candidates_and_safe_fields(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    report = scan_inbox(inbox, moment=MOMENT)
    assert report.packages == ()
    assert report.preflight_pass == () and report.quarantined == ()
    assert report.discovered == () and report.already_pending == ()
    assert report.requires_human_action is True
    assert report.has_candidates is False
    payload = report.to_dict()
    assert payload["blocker_code"] == PHASE3_3_BLOCKER_CODE
    assert payload["blocker_active"] is True
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["requires_human_action"] is True
    assert payload["counts"] == {
        "packages": 0,
        "discovered": 0,
        "already_pending": 0,
        "status_changed": 0,
        "preflight_pass": 0,
        "quarantined": 0,
        "skipped": 0,
        "pending_entries": 0,
    }


def test_missing_inbox_dir_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(InboxDirError):
        scan_inbox(tmp_path / "no-such-inbox", moment=MOMENT)


def test_naive_moment_is_rejected(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    with pytest.raises(ValueError):
        scan_inbox(inbox, moment=datetime(2026, 9, 23, 0, 0))  # 禁止隐式时区


def test_valid_jsonl_candidate_passes_preflight(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, rows=[author_row("a-1"), author_row("a-2")])
    report = scan_inbox(inbox, moment=MOMENT)
    package = only_package(report)
    assert package.status is InboxStatus.PREFLIGHT_PASS
    assert package.reason_codes == () and package.reasons == ()
    assert len(package.fingerprint) == 64 and package.fingerprint in report.discovered
    assert package.evidence_type == "author"
    assert package.source == "vendor-author"
    assert package.rows == 2 and package.acceptable_rows == 2
    assert package.quarantined_rows == 0 and package.not_oos_eligible_rows == 0
    assert [item.digest_verified for item in package.files] == [True]
    assert all(item.sha256 == item.declared_sha256 for item in package.files)
    assert report.register.entries[0].fingerprint == package.fingerprint
    payload = status_of(report)
    assert payload["counts"]["preflight_pass"] == 1
    assert payload["preflight_pass"][0]["requires_human_action"] is True
    assert payload["quarantined"] == []
    # 通过预检也**不代表**资格通过：安全字段一字不改
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False


def test_valid_csv_candidate_with_provider_alias_passes_preflight(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "news-csv"
    package_dir.mkdir(parents=True)
    rows = [
        author_row("n-1", source="vendor-news", author_name="", external_account_id=""),
        author_row("n-2", source="vendor-news", author_name="", external_account_id=""),
    ]
    evidence = package_dir / "news.csv"
    write_rows(evidence, rows, fmt="csv")
    manifest = manifest_payload(evidence, evidence_name="news.csv")
    manifest.pop("source")
    manifest["provider"] = "vendor-news"  # source / provider 之一即可
    manifest["evidence_type"] = "news"
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    report = scan_inbox(inbox, moment=MOMENT)
    package = only_package(report)
    assert package.status is InboxStatus.PREFLIGHT_PASS
    assert package.source == "vendor-news" and package.evidence_type == "news"
    assert package.files[0].format == "csv"
    assert report.discovered == (package.fingerprint,)


def test_row_level_failures_reuse_gateway_reason_codes(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, rows=[author_row("bad-1", authorization_status="PENDING")])
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.CONTAINS_QUARANTINED_ROWS.value in package.reason_codes
    # 行级原因码与 Evidence Gateway 完全同源（同一字符串、同一计数）
    assert package.code_counts == ((InboxReasonCode.AUTHORIZATION_MISSING.value, 1),)
    assert package.quarantined_rows == 1 and package.acceptable_rows == 0


def test_row_without_availability_evidence_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(
        inbox,
        fmt="csv",
        evidence_name="author.csv",
        rows=[author_row("no-avail", available_at="")],
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    # 缺独立可用性证据 → 复用既有资格类原因码（不新造阈值、不降低要求）
    assert InboxReasonCode.AVAILABILITY_UNPROVEN.value in package.reason_codes


def test_historical_oos_not_applicable_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-no-oos"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["historical_oos_applicable"] = False
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert package.reason_codes == (InboxReasonCode.AVAILABILITY_UNPROVEN.value,)


def test_missing_manifest_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "evidence-without-manifest"
    package_dir.mkdir(parents=True)
    (package_dir / EVIDENCE_NAME).write_text("{}\n", encoding="utf-8")
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert package.reason_codes == (InboxReasonCode.MANIFEST_MISSING.value,)
    assert len(package.fingerprint) == 64  # 无 manifest 也有内容级指纹（幂等）


def test_loose_root_file_is_quarantined_not_silently_ignored(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "news.csv").write_text("source\nx\n", encoding="utf-8")
    report = scan_inbox(inbox, moment=MOMENT)
    package = only_package(report)
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.MANIFEST_MISSING.value in package.reason_codes
    # 未被移动 / 删除：原文件仍在原处
    assert (inbox / "news.csv").read_text(encoding="utf-8") == "source\nx\n"



@pytest.mark.parametrize("broken", ['{ not json', '["not-an-object"]'])
def test_corrupt_manifest_is_fail_closed(tmp_path: Path, broken: str) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, manifest=broken)
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert package.reason_codes == (InboxReasonCode.MANIFEST_UNREADABLE.value,)


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("evidence_type", InboxReasonCode.MANIFEST_FIELD_MISSING),
        ("source", InboxReasonCode.MANIFEST_FIELD_MISSING),
        ("authorization_reference", InboxReasonCode.AUTHORIZATION_MISSING),
        ("time_semantics", InboxReasonCode.TIME_SEMANTICS_MISSING),
        ("availability_semantics", InboxReasonCode.AVAILABILITY_SEMANTICS_MISSING),
        ("historical_oos_applicable", InboxReasonCode.MANIFEST_FIELD_MISSING),
        ("files", InboxReasonCode.MANIFEST_FIELD_MISSING),
    ],
)
def test_missing_required_manifest_declarations_are_fail_closed(
    tmp_path: Path, field: str, expected_code: InboxReasonCode
) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-missing-field"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    assert field in inbox_module.MANIFEST_REQUIRED_FIELDS
    manifest.pop(field)
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert expected_code.value in package.reason_codes


def test_unsupported_evidence_type_and_schema_version_are_fail_closed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-bad-type"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["evidence_type"] = "macro"
    manifest["schema_version"] = 99
    manifest["contract_version"] = "evidence-intake-v0"
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.EVIDENCE_TYPE_UNSUPPORTED.value in package.reason_codes
    assert InboxReasonCode.MANIFEST_SCHEMA_UNSUPPORTED.value in package.reason_codes


def test_invalid_authorization_reference_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-bad-ref"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["authorization_reference"] = "http://vendor.example/terms"
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    # 复用 Evidence Gateway 的引用校验口径（非 https 一律不接受）
    assert package.reason_codes == (InboxReasonCode.AUTHORIZATION_REFERENCE_INVALID.value,)


def test_digest_mismatch_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, rows=[author_row()])
    manifest_path = inbox / "pkg-author-01" / MANIFEST_FILE_NAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.EVIDENCE_DIGEST_MISMATCH.value in package.reason_codes
    # 摘要不一致时**不解析**内容（fail-closed 短路），故没有行级计数
    assert package.rows == 0 and package.acceptable_rows == 0


def test_digest_missing_or_malformed_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, rows=[author_row()])
    manifest_path = inbox / "pkg-author-01" / MANIFEST_FILE_NAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["files"][0]["sha256"] = "not-a-digest"
    manifest_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.EVIDENCE_DIGEST_INVALID.value in package.reason_codes


def test_undeclared_extra_file_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox, rows=[author_row()])
    (package_dir / "extra-notes.txt").write_text("sidecar", encoding="utf-8")
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.EVIDENCE_FILE_UNDECLARED.value in package.reason_codes



@pytest.mark.parametrize(
    "declared_path",
    [
        "../outside.jsonl",
        "..\\outside.jsonl",
        "nested/outside.jsonl",
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "\\\\server\\share\\x.jsonl",
    ],
)
def test_path_traversal_and_absolute_paths_are_rejected(
    tmp_path: Path, declared_path: str
) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-escape"
    package_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    write_rows(outside, [author_row()], fmt="jsonl")
    manifest = manifest_payload(outside, evidence_name=declared_path)
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.EVIDENCE_FILE_PATH_UNSAFE.value in package.reason_codes
    # 越界内容**从未**被读取：目录外的文件保持原样，且没有任何声明文件被接受
    assert outside.exists() and package.files == () and package.rows == 0


@pytest.mark.parametrize("declared_path", ["", "   "])
def test_empty_declared_path_is_rejected(tmp_path: Path, declared_path: str) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-empty-path"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["files"][0]["path"] = declared_path
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.EVIDENCE_FILE_ENTRY_INVALID.value in package.reason_codes


def test_symlinked_evidence_file_is_rejected_without_following(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-symlink"
    package_dir.mkdir(parents=True)
    real = tmp_path / "real-author.jsonl"
    write_rows(real, [author_row()], fmt="jsonl")
    link = package_dir / EVIDENCE_NAME
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows 权限
        pytest.skip(f"本环境不允许创建符号链接：{exc}")
    manifest = manifest_payload(real)
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.EVIDENCE_FILE_PATH_UNSAFE.value in package.reason_codes
    assert package.files == () and package.rows == 0  # 绝不被跟随 / 绝不解析


def test_symlinked_package_dir_is_skipped_without_following(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside-package"
    build_package(tmp_path, name="outside-package", rows=[author_row()])
    link = inbox / "linked-package"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows 权限
        pytest.skip(f"本环境不允许创建符号链接：{exc}")
    report = scan_inbox(inbox, moment=MOMENT)
    assert report.packages == ()
    assert report.skipped == (("linked-package", InboxReasonCode.PACKAGE_SYMLINK_REJECTED.value),)
    assert report.to_dict()["counts"]["skipped"] == 1


@pytest.mark.parametrize("marker", ["is_mock", "record_kind", "is_example", "synthetic"])
def test_synthetic_example_package_never_passes_preflight(tmp_path: Path, marker: str) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-marked"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    row = author_row()
    row.update({marker: "example" if marker == "record_kind" else "true"})
    write_rows(evidence, [row], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest[marker] = "example" if marker == "record_kind" else "true"
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.SYNTHETIC_EVIDENCE.value in package.reason_codes
    assert package.synthetic is True


def test_example_named_package_is_quarantined(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, name="author-sample", rows=[author_row()])
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.SYNTHETIC_EVIDENCE.value in package.reason_codes


def test_empty_evidence_file_has_no_acceptable_rows(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, rows=[author_row()])
    package_dir = inbox / "pkg-author-01"
    evidence = package_dir / EVIDENCE_NAME
    evidence.write_text("", encoding="utf-8")
    manifest = json.loads((package_dir / MANIFEST_FILE_NAME).read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = hashing.sha256_bytes(b"")
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.NO_ACCEPTABLE_ROWS.value in package.reason_codes
    assert package.rows == 0 and package.acceptable_rows == 0


def test_partially_quarantined_package_is_fail_closed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(
        inbox,
        rows=[author_row("good-1"), author_row("good-2", published_at="not-a-time")],
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.CONTAINS_QUARANTINED_ROWS.value in package.reason_codes
    assert package.rows == 2 and package.acceptable_rows == 1
    assert package.quarantined_rows == 1



def test_rescan_is_idempotent_and_does_not_duplicate_pending_items(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "inbox_pending.json"
    build_package(inbox, rows=[author_row()])
    first = run_inbox_scan(inbox, moment=MOMENT, out_path=out)
    assert len(first.discovered) == 1 and first.already_pending == ()
    register = load_pending_register(out)
    assert register is not None and len(register.entries) == 1
    entry = register.entries[0]
    assert entry.scan_count == 1 and entry.first_seen_at == MOMENT.isoformat()

    # 同内容重复扫描（不同审计时点）：不重复生成待处理项，仅更新 last_seen / scan_count
    second = run_inbox_scan(inbox, moment=LATER, out_path=out, pending_path=out)
    assert second.discovered == ()
    assert second.already_pending == (first.discovered[0],)
    assert second.to_dict()["counts"]["discovered"] == 0
    assert second.to_dict()["counts"]["pending_entries"] == 1
    updated = load_pending_register(out)
    assert updated is not None and len(updated.entries) == 1  # 幂等：条目数不增长
    assert updated.entries[0].first_seen_at == MOMENT.isoformat()
    assert updated.entries[0].last_seen_at == LATER.isoformat()
    assert updated.entries[0].scan_count == 2
    assert [item.name for item in out.parent.iterdir() if item.name.endswith(".tmp")] == []


def test_content_change_yields_new_fingerprint_and_new_pending_item(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "inbox_pending.json"
    package_dir = build_package(inbox, rows=[author_row("a-1")])
    first = run_inbox_scan(inbox, moment=MOMENT, out_path=out)
    original = first.discovered[0]

    # 内容变化：追加一行并同步更新 manifest 摘要
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row("a-1"), author_row("a-2")], fmt="jsonl")
    manifest_path = package_dir / MANIFEST_FILE_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = hashing.sha256_bytes(evidence.read_bytes())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    second = run_inbox_scan(inbox, moment=LATER, out_path=out, pending_path=out)
    assert len(second.discovered) == 1
    assert second.discovered[0] != original  # 内容变化 → 新指纹（绝不静默复用旧身份）
    assert second.status_changed == ()
    register = load_pending_register(out)
    assert register is not None and len(register.entries) == 1  # pending = 当前 inbox 内容镜像
    assert register.entries[0].fingerprint == second.discovered[0]
    assert register.entries[0].status == InboxStatus.PREFLIGHT_PASS.value
    assert original not in {item.fingerprint for item in register.entries}


def test_status_change_for_same_content_is_reported(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "inbox_pending.json"
    # 授权核验时间在"较早审计时点"之后 → 早看是"未来时间"（隔离）；晚看则合规
    build_package(
        inbox,
        rows=[author_row("late-review", authorization_reviewed_at="2026-06-05T00:00:00+00:00")],
    )
    early = datetime(2026, 6, 3, tzinfo=UTC)
    first = run_inbox_scan(inbox, moment=early, out_path=out)
    assert first.status_changed == ()
    first_register = load_pending_register(out)
    assert first_register is not None
    assert first_register.entries[0].status == InboxStatus.QUARANTINED.value

    second = run_inbox_scan(inbox, moment=LATER, out_path=out, pending_path=out)
    assert second.discovered == ()  # 内容未变：指纹不变（幂等）
    assert second.already_pending == (first.discovered[0],)
    assert second.status_changed == (first.discovered[0],)  # 状态变化必须显式报出
    assert second.to_dict()["counts"]["status_changed"] == 1
    assert second.to_dict()["counts"]["pending_entries"] == 1  # 不重复生成待处理项
    updated = load_pending_register(out)
    assert updated is not None and updated.entries[0].status == InboxStatus.PREFLIGHT_PASS.value
    assert updated.entries[0].first_seen_at == early.isoformat()


def test_fingerprint_ignores_mtime_and_scan_time(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = build_package(inbox, rows=[author_row()])
    at_moment = only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint
    later = only_package(scan_inbox(inbox, moment=LATER)).fingerprint
    assert at_moment == later  # 扫描时间不参与指纹

    evidence = package_dir / EVIDENCE_NAME
    os.utime(evidence, (0, 0))  # 改 mtime（内容不变）
    manifest = package_dir / MANIFEST_FILE_NAME
    os.utime(manifest, (0, 0))
    assert only_package(scan_inbox(inbox, moment=MOMENT)).fingerprint == at_moment



def test_pending_register_is_written_atomically_and_is_self_describing(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "state" / "inbox_pending.json"
    build_package(inbox, rows=[author_row()])
    report = run_inbox_scan(inbox, moment=MOMENT, out_path=out)
    assert out.exists()
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["kind"] == PENDING_KIND
    assert document["blocker_active"] is True and document["human_gate_required"] is True
    assert document["data_qualification_passed"] is False
    assert document["phase_transition_allowed"] is False
    assert document["entry_count"] == 1
    assert document["generated_at"] == MOMENT.isoformat()
    assert report.to_dict()["pending"]["entries"] == document["entries"]
    assert [item.name for item in out.parent.iterdir() if item.name.endswith(".tmp")] == []
    assert (out.parent / (out.name + ".lock")).exists()  # 锁文件留痕（不删除）


def test_output_inside_inbox_is_rejected_before_writing(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    build_package(inbox, rows=[author_row()])
    with pytest.raises(InboxDirError):
        run_inbox_scan(inbox, moment=MOMENT, out_path=inbox / "pending.json")
    assert not (inbox / "pending.json").exists()
    assert not (inbox / "pending.json.lock").exists()


def test_corrupt_pending_state_fails_closed_without_writing(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    state = tmp_path / "inbox_pending.json"
    out = tmp_path / "out_pending.json"
    build_package(inbox, rows=[author_row()])
    state.write_text("{ not json", encoding="utf-8")
    with pytest.raises(PendingStateError):
        load_pending_register(state)
    with pytest.raises(PendingStateError):
        run_inbox_scan(inbox, moment=MOMENT, pending_path=state, out_path=out)
    assert state.read_text(encoding="utf-8") == "{ not json"  # 旧文件原样保留
    assert not out.exists()


def test_pending_state_cannot_weaken_hardcoded_safety_fields(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    state = tmp_path / "inbox_pending.json"
    build_package(inbox, rows=[author_row()])
    run_inbox_scan(inbox, moment=MOMENT, out_path=state)
    document = json.loads(state.read_text(encoding="utf-8"))
    document["blocker_active"] = False
    document["data_qualification_passed"] = True
    state.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    report = run_inbox_scan(inbox, moment=LATER, pending_path=state)
    payload = report.to_dict()
    assert payload["blocker_active"] is True  # 硬编码，绝不被 state 透传
    assert payload["human_gate_required"] is True
    assert payload["data_qualification_passed"] is False
    assert payload["phase_transition_allowed"] is False
    assert payload["pending"]["data_qualification_passed"] is False


def test_lock_conflict_is_fail_closed_without_writing(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "inbox_pending.json"
    build_package(inbox, rows=[author_row()])
    lock_path = out.with_name(out.name + ".lock")
    with SingleInstanceLock(lock_path, owner="holder") as info:
        assert info.owner == "holder"
        with pytest.raises(LockConflictError):
            run_inbox_scan(inbox, moment=MOMENT, out_path=out)
        assert not out.exists()  # 零写入
        assert lock_path.exists()  # 活动锁不被删除 / 改写


def test_outputs_are_redacted_and_no_secret_leaks(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "inbox_pending.json"
    package_dir = inbox / "author-secrets"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["authorization_reference"] = f"https://vendor.example/terms?api_key={SECRET}"
    manifest["notes"] = f"token={SECRET}"
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    report = run_inbox_scan(inbox, moment=MOMENT, out_path=out)
    blob = (
        json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        + render_inbox_summary(report)
        + out.read_text(encoding="utf-8")
    )
    assert SECRET not in blob  # 凭据类值一律不落盘 / 不打印
    assert InboxReasonCode.SENSITIVE_VALUE_DETECTED.value in blob  # 只记录键名
    assert "https://vendor.example/terms" in blob  # URL 查询串被剥离
    assert "api_key=***" not in blob and "token=***" not in blob


def test_sensitive_manifest_key_is_quarantined_by_key_name_only(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    package_dir = inbox / "author-keyname"
    package_dir.mkdir(parents=True)
    evidence = package_dir / EVIDENCE_NAME
    write_rows(evidence, [author_row()], fmt="jsonl")
    manifest = manifest_payload(evidence)
    manifest["api_key"] = SECRET
    (package_dir / MANIFEST_FILE_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    package = only_package(scan_inbox(inbox, moment=MOMENT))
    assert package.status is InboxStatus.QUARANTINED
    assert InboxReasonCode.SENSITIVE_VALUE_DETECTED.value in package.reason_codes
    assert SECRET not in json.dumps(package.to_dict(), ensure_ascii=False)


def test_render_summary_keeps_blocker_and_human_gate_visible(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    build_package(inbox, rows=[author_row()])
    build_package(inbox, name="news-broken", rows=[author_row("x", authorization_status="PENDING")])
    report = scan_inbox(inbox, moment=MOMENT)
    summary = render_inbox_summary(report)
    assert PHASE3_3_BLOCKER_CODE in summary
    assert "human_gate_required=true" in summary
    assert "不自动 intake" in summary
    assert "pkg-author-01" in summary
    assert "news-broken" in summary
    assert InboxReasonCode.CONTAINS_QUARANTINED_ROWS.value in summary


def test_module_has_no_network_database_or_destructive_behaviour() -> None:
    source = Path(inbox_module.__file__).read_text(encoding="utf-8")
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
    # 无网络 / 无数据库 / 无常驻循环
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))

