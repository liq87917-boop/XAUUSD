"""GOLD-051：CONTROL_PLANE_FRESH_PROCESS_RESTART 的机读证据 schema 与只读校验器。

把 fresh-process 门禁证据从自由文本改为版本化、可机读、内容寻址的状态文件。
本模块**只读**：判定 cleared / not_cleared / invalid 并输出稳定 reason codes；
绝不写文件、绝不解除 gate、绝不产生 verdict、绝不调用网络（仅本地 git 元数据）。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "fresh-process-restart-evidence-v1"

# 与 PROJECT_STATE.blocker 一致的最小 restart 快照
MIN_RESTART_SNAPSHOT = "490c1decf1ba4b316019c7a272726d04601b8fab"

VERDICT_CLEARED = "cleared"
VERDICT_NOT_CLEARED = "not_cleared"
VERDICT_INVALID = "invalid"

REASON_HEAD_MISMATCH = "HEAD_MISMATCH"
REASON_RESTART_COMMIT_PRECEDES_MIN_SNAPSHOT = "RESTART_COMMIT_PRECEDES_MIN_SNAPSHOT"
REASON_CI_NOT_GREEN = "CI_NOT_GREEN"
REASON_MISSING_OPERATOR_IDENTITY = "MISSING_OPERATOR_IDENTITY"
REASON_MISSING_HUMAN_ATTESTATION = "MISSING_HUMAN_ATTESTATION"
REASON_DIGEST_MISMATCH = "DIGEST_MISMATCH"
REASON_INVALID = "INVALID"

# 内容寻址摘要排除的瞬时字段：时间戳与自引用摘要本身。排除时间戳后，
# 同一份事实（相同 operator / commit / CI run）即便在不同时刻重新采集，摘要也稳定。
_TRANSIENT_FIELDS = frozenset(
    {"content_digest", "new_process_started_at", "attested_at", "terminated_at"}
)

REQUIRED_FIELDS = (
    "operator_identity",
    "restart_commit_sha",
    "old_pid_terminated",
    "new_process_pid",
    "new_process_started_at",
    "head_check_cmd_and_output",
    "git_fetch_or_pull_cmd_and_output",
    "ci_run_id",
    "ci_run_conclusion_on_restart_commit",
    "human_attestation",
)


def schema() -> dict[str, Any]:
    """返回版本化 schema 契约（只读）。"""
    return {
        "schema": SCHEMA_VERSION,
        "schema_version": 1,
        "required": list(REQUIRED_FIELDS),
        "optional": ["content_digest"],
        "old_pid_terminated": "list of {pid, method, terminated_at}",
        "verdicts": [VERDICT_CLEARED, VERDICT_NOT_CLEARED, VERDICT_INVALID],
        "reason_codes": [
            REASON_HEAD_MISMATCH,
            REASON_RESTART_COMMIT_PRECEDES_MIN_SNAPSHOT,
            REASON_CI_NOT_GREEN,
            REASON_MISSING_OPERATOR_IDENTITY,
            REASON_MISSING_HUMAN_ATTESTATION,
            REASON_DIGEST_MISMATCH,
        ],
    }


def _strip_transient(obj: Any) -> Any:
    """递归剔除瞬时字段（时间戳与自引用摘要），用于内容寻址摘要。"""
    if isinstance(obj, dict):
        return {
            key: _strip_transient(value)
            for key, value in obj.items()
            if key not in _TRANSIENT_FIELDS
        }
    if isinstance(obj, list):
        return [_strip_transient(item) for item in obj]
    return obj


def compute_content_digest(data: dict[str, Any]) -> str:
    """对证据事实内容求 sha256 摘要（排除时间戳与自引用摘要字段）。

    使状态文件「内容寻址、除时间戳外摘要稳定」：同一 candidate 输入两次，
    只要事实一致，摘要必然相同。
    """
    canonical = json.dumps(
        _strip_transient(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _git_rev_parse(repo: Path, ref: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return False
    return out.returncode == 0


def verify_evidence(
    evidence_path: Path,
    repo: Path,
    branch: str = "cline-agent",
) -> dict[str, Any]:
    """只读校验候选 evidence 文件。

    返回 {"verdict": cleared|not_cleared|invalid, "reason_codes": [...], "detail": str}。
    """
    if not evidence_path.exists():
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_INVALID],
            "detail": "missing evidence file",
        }

    try:
        data = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_INVALID],
            "detail": f"unreadable: {exc}",
        }

    if not isinstance(data, dict):
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_INVALID],
            "detail": "not a JSON object",
        }

    if data.get("schema") != SCHEMA_VERSION:
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_INVALID],
            "detail": f"schema mismatch: {data.get('schema')!r}",
        }

    missing = [f for f in REQUIRED_FIELDS if f not in data or data.get(f) is None]
    if missing:
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_INVALID],
            "detail": f"missing fields: {missing}",
        }

    # 内容寻址自校验（可选字段）：存在但与被摘要内容不一致 ⇒ 证据被篡改 / 损坏。
    stored_digest = data.get("content_digest")
    if stored_digest is not None and str(stored_digest) != compute_content_digest(data):
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_DIGEST_MISMATCH],
            "detail": "",
        }

    restart_sha = str(data.get("restart_commit_sha", "")).strip()
    local_head = _git_rev_parse(repo, "HEAD")
    remote_head = _git_rev_parse(repo, f"origin/{branch}")

    # 指向错误 / 过旧的 restart commit ⇒ 证据本身无效（不是 gate 未满足）。
    if restart_sha != local_head or restart_sha != remote_head:
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_HEAD_MISMATCH],
            "detail": "",
        }
    if not _is_ancestor(repo, MIN_RESTART_SNAPSHOT, restart_sha):
        return {
            "verdict": VERDICT_INVALID,
            "reason_codes": [REASON_RESTART_COMMIT_PRECEDES_MIN_SNAPSHOT],
            "detail": "",
        }

    # 以下为「证据合法但 gate 条件未满足」⇒ not_cleared。
    reasons: list[str] = []

    if not str(data.get("operator_identity", "")).strip():
        reasons.append(REASON_MISSING_OPERATOR_IDENTITY)

    attestation = data.get("human_attestation")
    if not isinstance(attestation, dict) or not str(attestation.get("statement", "")).strip():
        reasons.append(REASON_MISSING_HUMAN_ATTESTATION)

    conclusion = str(data.get("ci_run_conclusion_on_restart_commit", "")).strip().lower()
    if conclusion != "success":
        reasons.append(REASON_CI_NOT_GREEN)

    if reasons:
        return {
            "verdict": VERDICT_NOT_CLEARED,
            "reason_codes": sorted(set(reasons)),
            "detail": "",
        }
    return {"verdict": VERDICT_CLEARED, "reason_codes": [], "detail": ""}
