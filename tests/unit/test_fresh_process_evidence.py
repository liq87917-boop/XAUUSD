"""GOLD-051：fresh-process 机读证据 schema + 校验器 + CLI 的单元测试。

覆盖：
1. verify_evidence：cleared / not_cleared（HEAD_MISMATCH、CI_NOT_GREEN、MIN_SNAPSHOT）/
   invalid（缺字段、schema 不匹配）；
2. CLI：CI 环境 fail-closed 非零退出且不写文件；非 CI 环境写 schema 合规 JSON 并自校验。
"""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator import fresh_process_evidence as fpe


def _evidence(**overrides) -> dict:
    data = {
        "schema": fpe.SCHEMA_VERSION,
        "schema_version": 1,
        "operator_identity": "human@example.com",
        "restart_commit_sha": "abc123def456",
        "old_pid_terminated": [
            {"pid": 123, "method": "SIGTERM", "terminated_at": "2026-09-25T00:00:00+00:00"}
        ],
        "new_process_pid": 456,
        "new_process_started_at": "2026-09-25T00:00:00+00:00",
        "head_check_cmd_and_output": "git rev-parse HEAD\nHEAD=abc123def456",
        "git_fetch_or_pull_cmd_and_output": "git pull --ff-only origin cline-agent",
        "ci_run_id": "36130656107",
        "ci_run_conclusion_on_restart_commit": "success",
        "human_attestation": {
            "statement": "I attest to the fresh-process restart.",
            "attested_at": "2026-09-25T00:00:00+00:00",
            "operator_identity": "human@example.com",
        },
    }
    data.update(overrides)
    return data


def _mock_git(monkeypatch, *, head: str = "abc123def456", ancestor: bool = True) -> None:
    monkeypatch.setattr(fpe, "_git_rev_parse", lambda repo, ref: head)
    monkeypatch.setattr(fpe, "_is_ancestor", lambda repo, a, d: ancestor)


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "fresh_process_restart_evidence.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_verify_cleared(monkeypatch, tmp_path) -> None:
    _mock_git(monkeypatch)
    result = fpe.verify_evidence(_write(tmp_path, _evidence()), tmp_path)
    assert result["verdict"] == fpe.VERDICT_CLEARED
    assert result["reason_codes"] == []


def test_verify_head_mismatch(monkeypatch, tmp_path) -> None:
    # evidence 的 restart_commit_sha 与本地 HEAD 不一致 ⇒ 证据无效
    _mock_git(monkeypatch, head="other999")
    result = fpe.verify_evidence(_write(tmp_path, _evidence()), tmp_path)
    assert result["verdict"] == fpe.VERDICT_INVALID
    assert fpe.REASON_HEAD_MISMATCH in result["reason_codes"]


def test_verify_precedes_min_snapshot(monkeypatch, tmp_path) -> None:
    # restart_commit_sha 不是 MIN_RESTART_SNAPSHOT 的后代 ⇒ 证据无效
    _mock_git(monkeypatch, ancestor=False)
    result = fpe.verify_evidence(_write(tmp_path, _evidence()), tmp_path)
    assert result["verdict"] == fpe.VERDICT_INVALID
    assert fpe.REASON_RESTART_COMMIT_PRECEDES_MIN_SNAPSHOT in result["reason_codes"]


def test_verify_ci_not_green(monkeypatch, tmp_path) -> None:
    _mock_git(monkeypatch)
    result = fpe.verify_evidence(
        _write(tmp_path, _evidence(ci_run_conclusion_on_restart_commit="failure")), tmp_path
    )
    assert result["verdict"] == fpe.VERDICT_NOT_CLEARED
    assert fpe.REASON_CI_NOT_GREEN in result["reason_codes"]


def test_verify_missing_field(monkeypatch, tmp_path) -> None:
    _mock_git(monkeypatch)
    data = _evidence()
    del data["operator_identity"]
    result = fpe.verify_evidence(_write(tmp_path, data), tmp_path)
    assert result["verdict"] == fpe.VERDICT_INVALID


def test_verify_schema_mismatch(monkeypatch, tmp_path) -> None:
    _mock_git(monkeypatch)
    result = fpe.verify_evidence(
        _write(tmp_path, _evidence(schema="wrong-schema")), tmp_path
    )
    assert result["verdict"] == fpe.VERDICT_INVALID


def test_verify_empty_old_pid_list_is_valid(monkeypatch, tmp_path) -> None:
    # 回归：old_pid_terminated=[]（空列表）是合法值，不得被误判为「缺失字段」
    _mock_git(monkeypatch)
    result = fpe.verify_evidence(
        _write(tmp_path, _evidence(old_pid_terminated=[])), tmp_path
    )
    assert result["verdict"] == fpe.VERDICT_CLEARED


def test_verify_empty_operator_identity_blocked(monkeypatch, tmp_path) -> None:
    # 回归：operator_identity="" 仍应在 REASON_MISSING_OPERATOR_IDENTITY 被拦下（.strip() 判断）
    _mock_git(monkeypatch)
    result = fpe.verify_evidence(
        _write(tmp_path, _evidence(operator_identity="")), tmp_path
    )
    assert result["verdict"] == fpe.VERDICT_NOT_CLEARED
    assert fpe.REASON_MISSING_OPERATOR_IDENTITY in result["reason_codes"]


# ---- CLI ----

def _import_cli():
    from scripts import record_fresh_process_evidence

    return record_fresh_process_evidence


def test_cli_rejects_ci_environment(monkeypatch, tmp_path) -> None:
    cli = _import_cli()
    out_path = tmp_path / "evidence.json"
    monkeypatch.setattr(cli, "EVIDENCE_PATH", out_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CI", raising=False)

    code = cli.main(["--input", json.dumps(_evidence())])

    assert code == cli.EXIT_CI_FORBIDDEN
    assert not out_path.exists()  # 不写文件


def test_cli_writes_valid_json(monkeypatch, tmp_path) -> None:
    cli = _import_cli()
    out_path = tmp_path / "evidence.json"
    monkeypatch.setattr(cli, "EVIDENCE_PATH", out_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    # 让 CLI 的自校验（verify_evidence）通过
    monkeypatch.setattr(cli, "_git_rev_parse", lambda ref: "abc123def456")
    monkeypatch.setattr(
        fpe, "_git_rev_parse", lambda repo, ref: "abc123def456"
    )
    monkeypatch.setattr(fpe, "_is_ancestor", lambda repo, a, d: True)

    code = cli.main(["--input", json.dumps(_evidence())])

    assert code == cli.EXIT_OK
    assert out_path.exists()
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["schema"] == fpe.SCHEMA_VERSION
    assert written["operator_identity"] == "human@example.com"

def test_verify_missing_human_attestation(monkeypatch, tmp_path) -> None:
    _mock_git(monkeypatch)
    data = _evidence()
    data["human_attestation"] = {
        "statement": "",
        "attested_at": "2026-09-25T00:00:00+00:00",
        "operator_identity": "human@example.com",
    }
    result = fpe.verify_evidence(_write(tmp_path, data), tmp_path)
    assert result["verdict"] == fpe.VERDICT_NOT_CLEARED
    assert fpe.REASON_MISSING_HUMAN_ATTESTATION in result["reason_codes"]


def test_verify_digest_mismatch(monkeypatch, tmp_path) -> None:
    # 内容寻址：摘要与内容不一致 ⇒ 证据被篡改 / 损坏，判 invalid。
    _mock_git(monkeypatch)
    data = _evidence()
    data["content_digest"] = "0" * 64
    result = fpe.verify_evidence(_write(tmp_path, data), tmp_path)
    assert result["verdict"] == fpe.VERDICT_INVALID
    assert fpe.REASON_DIGEST_MISMATCH in result["reason_codes"]


def test_compute_content_digest_stable_across_timestamps() -> None:
    # 回归（req 6 case 5）：同一 candidate 输入两次，除时间戳外摘要稳定。
    a = _evidence()
    b = _evidence()
    b["new_process_started_at"] = "2026-09-26T00:00:00+00:00"
    b["human_attestation"]["attested_at"] = "2026-09-26T00:00:00+00:00"
    b["old_pid_terminated"][0]["terminated_at"] = "2026-09-26T00:00:00+00:00"
    assert fpe.compute_content_digest(a) == fpe.compute_content_digest(b)


def test_cli_writes_content_digest(monkeypatch, tmp_path) -> None:
    # CLI 写入的内容寻址摘要与自校验一致。
    cli = _import_cli()
    out_path = tmp_path / "evidence.json"
    monkeypatch.setattr(cli, "EVIDENCE_PATH", out_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(fpe, "_git_rev_parse", lambda repo, ref: "abc123def456")
    monkeypatch.setattr(fpe, "_is_ancestor", lambda repo, a, d: True)

    code = cli.main(["--input", json.dumps(_evidence())])

    assert code == cli.EXIT_OK
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["content_digest"] == fpe.compute_content_digest(written)


def test_production_evidence_path_untouched_by_cli(monkeypatch, tmp_path) -> None:
    # 回归（req 4）：CLI 只对「新建路径」负责，绝不触碰生产证据文件（若存在则逐字节不变）。
    cli = _import_cli()
    prod = cli.EVIDENCE_PATH
    before = prod.read_bytes() if prod.exists() else None
    out_path = tmp_path / "evidence.json"
    monkeypatch.setattr(cli, "EVIDENCE_PATH", out_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(fpe, "_git_rev_parse", lambda repo, ref: "abc123def456")
    monkeypatch.setattr(fpe, "_is_ancestor", lambda repo, a, d: True)

    code = cli.main(["--input", json.dumps(_evidence())])

    assert code == cli.EXIT_OK
    after = prod.read_bytes() if prod.exists() else None
    assert after == before  # 生产证据文件逐字节不变


def test_production_evidence_file_if_present_is_schema_valid() -> None:
    # 回归（req 4）：pytest 运行前后生产证据文件若存在，必须符合 schema（否则 fail）。
    # 本任务不删除已存在的人工文件，只对新建路径负责。
    cli = _import_cli()
    prod = cli.EVIDENCE_PATH
    if not prod.exists():
        return  # 允许不存在（gate 未清除）
    data = json.loads(prod.read_text(encoding="utf-8"))
    assert data.get("schema") == fpe.SCHEMA_VERSION
    for field in fpe.REQUIRED_FIELDS:
        assert field in data, f"production evidence missing field: {field}"
