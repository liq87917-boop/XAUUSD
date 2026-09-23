"""确定性 GPT Review Binding Manifest（GOLD-031）。

为什么需要它
------------
``.ai/DEVELOPMENT_PROTOCOL.md`` §2 规定任务 COMPLETED 之后必须由 **GPT** Review。
GOLD-025 已经把 Review 变成机器可审计的台账条目，但那条台账需要 GPT 手工填写
``reviewed_result.result_sha256`` 与 ``reviewed_commit.sha``。GPT 因此需要一个
**纯只读、确定性的内容身份清单**来取得这些事实，而不是让 Executor 去「代填」——
一旦 Executor 拥有生成 Review 结论的能力，GPT-only Review 就名存实亡。

本模块只输出**事实**（绝不输出结论）：

1. result 的**内容身份**：``sha256`` = 目标 commit 里 Git 存储（GitHub 提供的）canonical
   字节摘要（与 Review 台账 ``reviewed_result.result_sha256`` 同口径，可由独立 ``hashlib``
   复算），以及本地工作树原始字节摘要 ``worktree_sha256``；再加 ``status`` / ``finished_at`` /
   ``execution_outcome`` / attempt 计数；
2. task 文件内容身份（原始 bytes SHA-256 + 解析后的元数据摘要）；
3. 该 task 的**终态完成 commit** identity（``sha`` / ``branch`` / ``head`` /
   ``subject`` / ``committed_at``）：只从 **HEAD 可达历史**里按
   ``ai: complete <task_id>`` / ``ai: blocked <task_id>``（与
   ``ai_orchestrator.commit_task_result`` 同源）解析，**绝不猜测、绝不编造**；
4. task / result 在**该 commit 里的 blob** 与**当前工作树**的内容一致性（以 Git 自己的
   blob id 口径判定，``core.autocrlf`` 等换行转换不算漂移；真正不一致 ⇒ fail-closed）；
5. 该 result 里**已经记录的** validation 摘要（命令 / 返回码 / 是否超时，**不重新执行**）。

安全红线（与 ``.clinerules`` / ``.ai/DEVELOPMENT_PROTOCOL.md`` 一致）
--------------------------------------------------------------------
- 本模块**没有任何写入路径**：不写 review 台账 / 项目状态 / tasks / results，
  不 commit / push / reset / checkout（由源码守卫测试锁定）；
- **绝不签发 verdict**：输出里不存在 ``verdict`` / ``acceptance_summary`` /
  ``reviewed_at`` / ``reviewer``；``binding.facts_complete`` 只表示「事实是否齐全」，
  **不是** Review 结论，也绝不推进任何 review 指针；
- 缺失 / 损坏 / 漂移（result 缺失、JSON 损坏、task 与 result 身份不一致、
  未知 status、Git identity 不可解析、工作树版本与目标 commit 不一致）一律
  **fail-closed**，并给出稳定 reason code；
- 唯一外部进程调用是**只读** git（``log`` / ``ls-tree`` / ``cat-file blob`` 白名单），
  零网络、零数据库、零业务证据写入、零模型调用。

用法
----
.. code-block:: text

    python -m orchestrator.review_binding --task GOLD-028            # 只读 manifest 写 stdout
    python -m orchestrator.review_binding --task GOLD-028 --root .   # 指定仓库根

``stdout`` 是**纯 ASCII JSON**（机器通道，任意代码页都可安全读取）；``stderr`` 只放
人类可读的 issue 摘要（不参与机器解析）。
退出码：``0`` 事实齐全 / ``2`` fail-closed（缺事实 / 漂移 / 身份不可解析）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orchestrator import ai_orchestrator as orch
from orchestrator import planner_snapshot as planner
from orchestrator import result_terminal_consistency as terminal_consistency

ROOT = Path(__file__).resolve().parent.parent

# 版本化 CLI / JSON 契约：shape 变化必须同时 bump 字符串 schema 与整数 schema_version。
REVIEW_BINDING_SCHEMA = "gold-ai/review-binding-manifest/v1"

REVIEW_BINDING_SCHEMA_VERSION = 1

REVIEW_BINDING_AUTHORITY_SCHEMA = "gold-ai/review-binding-authority/v1"

# 终态 commit subject 契约：与 ``ai_orchestrator.commit_task_result`` **同源**。
TERMINAL_COMMIT_SUBJECT_TEMPLATES: dict[str, str] = {
    "completed": "ai: complete {task_id}",
    "blocked": "ai: blocked {task_id}",
}

# task_id 只允许安全字符（防路径穿越；``..`` 一律非法）。
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 被引用的 commit 必须是完整 40 位小写 SHA（缩写一律拒绝）。
FULL_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# Git blob id（sha1，40 位十六进制）——Git 视角的内容身份。
GIT_BLOB_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# 终态状态词表的人类可读提示（只用于 issue detail，不参与任何判定）。
TERMINAL_STATUS_HINT = " / ".join(sorted(orch.TERMINAL_RESULT_STATUSES))

GIT_LOG_FIELD_SEPARATOR = "\x1f"

GIT_LOG_RECORD_SEPARATOR = "\x1e"

GIT_LOG_FORMAT = (
    f"%H{GIT_LOG_FIELD_SEPARATOR}%cI{GIT_LOG_FIELD_SEPARATOR}%s{GIT_LOG_RECORD_SEPARATOR}"
)

# **只读** Git 子命令白名单：白名单以外的任何子命令一律拒绝执行。
GIT_READ_ONLY_SUBCOMMANDS = ("log", "ls-tree", "cat-file", "hash-object")

GIT_TIMEOUT_SECONDS = 30

# 「取不到 blob」的两种稳定原因（供 fail-closed 映射）。
GIT_BLOB_ABSENT = "GIT_BLOB_ABSENT"

GIT_BLOB_UNAVAILABLE = "GIT_BLOB_UNAVAILABLE"

# 不参与确定性 digest 的字段：
# - ``generated_at`` 是 wall-clock 审计字段，绝不作为内容身份；
# - ``facts_digest`` / ``determinism`` 由 facts 自身派生（排除以避免自引用）。
FACTS_EXCLUDED_KEYS = ("generated_at", "facts_digest", "determinism")

# ---------- 诊断 issue 词表（稳定字符串，供 GPT / 人工 grep 与测试断言） ----------

ISSUE_TASK_ID_INVALID = "TASK_ID_INVALID"
ISSUE_TASK_FILE_MISSING = "TASK_FILE_MISSING"
ISSUE_TASK_FILE_UNREADABLE = "TASK_FILE_UNREADABLE"
ISSUE_TASK_FILE_TASK_ID_MISMATCH = "TASK_FILE_TASK_ID_MISMATCH"
ISSUE_RESULT_MISSING = "RESULT_MISSING"
ISSUE_RESULT_UNREADABLE = "RESULT_UNREADABLE"
ISSUE_RESULT_TASK_ID_MISMATCH = "RESULT_TASK_ID_MISMATCH"
ISSUE_RESULT_STATUS_UNKNOWN = "RESULT_STATUS_UNKNOWN"
ISSUE_RESULT_NOT_TERMINAL = "RESULT_NOT_TERMINAL"
ISSUE_GIT_INFO_UNAVAILABLE = "GIT_INFO_UNAVAILABLE"
ISSUE_GIT_LOG_UNAVAILABLE = "GIT_LOG_UNAVAILABLE"
ISSUE_COMMIT_NOT_FOUND = "COMPLETION_COMMIT_NOT_FOUND"
ISSUE_COMMIT_AMBIGUOUS = "COMPLETION_COMMIT_AMBIGUOUS"
ISSUE_COMMIT_SHA_INVALID = "COMPLETION_COMMIT_SHA_INVALID"
ISSUE_RESULT_NOT_IN_COMMIT = "RESULT_NOT_IN_COMMIT"
ISSUE_TASK_NOT_IN_COMMIT = "TASK_NOT_IN_COMMIT"
ISSUE_WORKTREE_COMMIT_MISMATCH = "WORKTREE_COMMIT_MISMATCH"

# GOLD-039：终态一致性校验器里**已由本模块自己的 code 表达**的事实，避免重复上报。
# - ``status`` 缺失 / 未知 ⇒ 本模块 ``RESULT_STATUS_UNKNOWN``；
# - result 不是 JSON 对象 ⇒ 本模块 ``RESULT_UNREADABLE``。
DELEGATED_TERMINAL_CONSISTENCY_CODES = (
    terminal_consistency.REASON_STATUS_MISSING,
    terminal_consistency.REASON_STATUS_UNKNOWN,
    terminal_consistency.REASON_RESULT_INVALID,
)

SEVERITY_ERROR = "error"

EXIT_OK = 0

EXIT_DRIFT = 2

# ---------- 字段顺序契约（字段顺序稳定 = 输出可重复） ----------

MANIFEST_FIELD_ORDER = (
    "schema",
    "schema_version",
    "generated_at",
    "read_only",
    "task_id",
    "paths",
    "task",
    "result",
    "commit",
    "validation",
    "binding",
    "authority",
    "determinism",
    "issues",
    "summary",
    "facts_digest",
)

PATH_FIELD_ORDER = (
    "root",
    "tasks_dir",
    "results_dir",
    "task_file",
    "result_file",
)

TASK_FIELD_ORDER = (
    "path",
    "sha256",
    "bytes",
    "worktree_sha256",
    "worktree_bytes",
    "worktree_matches_commit",
    "task_id",
    "title",
    "type",
    "human_gate",
    "depends_on",
    "auto_start",
    "requires_human_approval",
    "max_attempts",
    "metadata_digest",
)

RESULT_FIELD_ORDER = (
    "path",
    "sha256",
    "bytes",
    "worktree_sha256",
    "worktree_bytes",
    "worktree_matches_commit",
    "task_id",
    "status",
    "known_status",
    "terminal",
    "execution_outcome",
    "normalized_finish_reason",
    "finished_at",
    "attempt_count",
    "max_attempts",
)

COMMIT_FIELD_ORDER = (
    "resolved",
    "reason_code",
    "sha",
    "branch",
    "head",
    "subject",
    "committed_at",
    "history_scope",
)

VALIDATION_FIELD_ORDER = (
    "status",
    "source",
    "attempt_count",
    "validation_count",
    "commands",
    "results",
    "digest",
)

BINDING_FIELD_ORDER = (
    "facts_complete",
    "reason_codes",
    "human_gate",
    "blocking_human_gate",
    "requires_human_approval",
)

# 输出里**绝不允许**出现的 Review 结论字段（GPT-only Review 的机器可测边界）。
FORBIDDEN_MANIFEST_KEYS = (
    "verdict",
    "acceptance_summary",
    "reviewed_at",
    "reviewer",
    "reviewer_role",
)

MANIFEST_ORDERING: dict[str, str] = {
    "top_level": "MANIFEST_FIELD_ORDER",
    "paths": "PATH_FIELD_ORDER",
    "task": "TASK_FIELD_ORDER",
    "result": "RESULT_FIELD_ORDER",
    "commit": "COMMIT_FIELD_ORDER",
    "validation": "VALIDATION_FIELD_ORDER",
    "binding": "BINDING_FIELD_ORDER",
    "validation.results": "result 文件 attempt 顺序 + 段内 validation 顺序",
    "validation.commands": "sorted unique",
    "issues": "sorted by (code, detail)",
    "binding.reason_codes": "sorted unique error codes",
}

# 注入点类型：测试可注入惰性 fake，完全零子进程。
CommitLogProvider = Callable[[Path], "tuple[list[dict[str, Any]] | None, str | None]"]

BlobIdReader = Callable[[Path, str, str], "tuple[str | None, str | None]"]

BlobBytesReader = Callable[[Path, str], "tuple[bytes | None, str | None]"]

WorktreeBlobIdReader = Callable[[Path, str], "tuple[str | None, str | None]"]


# ============================================================
# 只读 Git（唯一外部进程调用，白名单子命令）
# ============================================================


def run_git(root: Path, args: Sequence[str]) -> tuple[bytes | None, str | None]:
    """**只读**执行白名单内 Git 子命令；返回 ``(stdout_bytes, error)``。

    白名单外的任何子命令（``commit`` / ``push`` / ``reset`` / ``checkout`` ...）
    一律拒绝执行并返回 error（fail-closed），因此本函数在结构上不可能写仓库。
    """

    if not args or args[0] not in GIT_READ_ONLY_SUBCOMMANDS:
        return None, f"refused non-read-only git subcommand: {list(args[:1])}"

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )

    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git unavailable: {exc}"

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        return None, f"git exit {completed.returncode}: {stderr}"

    return completed.stdout, None


def git_commit_log(root: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """**只读**读取 HEAD 可达历史的 ``(sha, committed_at, subject)`` 事实。

    只走 ``git log``（默认从 ``HEAD`` 出发）⇒ 这里找到的 commit **必然**是 HEAD 的祖先，
    identity 天然可追溯到当前分支；解析不了 / 记录损坏一律 fail-closed。
    """

    raw, error = run_git(root, ["log", f"--format={GIT_LOG_FORMAT}"])

    if error is not None or raw is None:
        return None, error or "empty git log output"

    entries: list[dict[str, Any]] = []

    for record in raw.decode("utf-8", errors="replace").split(GIT_LOG_RECORD_SEPARATOR):
        text = record.strip()

        if not text:
            continue

        fields = text.split(GIT_LOG_FIELD_SEPARATOR)

        if len(fields) != 3:
            return None, f"malformed git log record: {text[:80]!r}"

        entries.append(
            {
                "sha": fields[0].strip(),
                "committed_at": fields[1].strip(),
                "subject": fields[2].strip(),
            }
        )

    return entries, None


def git_blob_id(root: Path, sha: str, relative_path: str) -> tuple[str | None, str | None]:
    """**只读**读取某 commit 内某路径的 Git blob id；取不到时返回稳定原因码。"""

    tree, error = run_git(root, ["ls-tree", "-z", sha, "--", relative_path])

    if error is not None or tree is None:
        return None, GIT_BLOB_UNAVAILABLE

    entries = [entry for entry in tree.split(b"\x00") if entry.strip()]

    if not entries:
        return None, GIT_BLOB_ABSENT

    fields = entries[0].split(b"\t", 1)[0].split()

    if len(fields) < 3:
        return None, GIT_BLOB_UNAVAILABLE

    blob_id = fields[2].decode("ascii", errors="replace").strip()

    if GIT_BLOB_ID_PATTERN.match(blob_id) is None:
        return None, GIT_BLOB_UNAVAILABLE

    return blob_id, None


def git_blob_content(root: Path, blob_id: str) -> tuple[bytes | None, str | None]:
    """**只读**读取 blob 的 canonical 内容 bytes（Git 存储 / GitHub 提供的字节）。"""

    if GIT_BLOB_ID_PATTERN.match(blob_id) is None:
        return None, GIT_BLOB_UNAVAILABLE

    payload, error = run_git(root, ["cat-file", "blob", blob_id])

    if error is not None or payload is None:
        return None, GIT_BLOB_UNAVAILABLE

    return payload, None


def git_worktree_blob_id(root: Path, relative_path: str) -> tuple[str | None, str | None]:
    """**只读**计算工作树文件在 Git 语义下的 blob id（含 clean filter / autocrlf）。

    用 Git 自己的内容口径比较「工作树版本 vs 目标 commit 版本」：
    ``core.autocrlf`` 等换行转换**不是**内容漂移，只有 Git 认为文件被改写才算漂移。
    绝不写对象（不带 ``-w``），因此对仓库零副作用。
    """

    raw, error = run_git(root, ["hash-object", "--", relative_path])

    if error is not None or raw is None:
        return None, GIT_BLOB_UNAVAILABLE

    value = raw.decode("ascii", errors="replace").strip()

    if GIT_BLOB_ID_PATTERN.match(value) is None:
        return None, GIT_BLOB_UNAVAILABLE

    return value, None


def terminal_commit_subjects(task_id: str) -> tuple[str, ...]:
    """该 task 的终态 commit subject 全集（与 ``ai_orchestrator`` 同源）。"""

    return tuple(
        template.format(task_id=task_id) for template in TERMINAL_COMMIT_SUBJECT_TEMPLATES.values()
    )


# ============================================================
# 通用只读工具（确定性）
# ============================================================


def make_issue(code: str, detail: str) -> dict[str, str]:
    """构造一条 fail-closed 诊断（只报告，绝不触发任何修复动作）。"""

    return {"code": code, "severity": SEVERITY_ERROR, "detail": detail}


def text_value(value: object) -> str | None:
    """非空字符串（strip 后）→ 字符串，否则 ``None``。"""

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def read_bytes(path: Path) -> tuple[bytes | None, str | None]:
    """**只读**原始文件 bytes（不存在 / 不可读 → ``(None, error)``，绝不创建文件）。"""

    try:
        return path.read_bytes(), None

    except OSError as exc:
        return None, str(exc)


def sha256_bytes(payload: bytes) -> str:
    """原始 bytes 的 SHA-256（内容身份唯一口径：绝不使用 mtime / 当前时间 / 模型输出）。"""

    return hashlib.sha256(payload).hexdigest()


def canonical_digest(payload: object) -> str:
    """确定性 JSON 摘要（固定 sort_keys + 紧凑分隔符 ⇒ 相同事实必然相同 digest）。"""

    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def relative_to_root(root: Path, path: Path) -> str | None:
    """``path`` 相对仓库根的 POSIX 形式（不在根内 → ``None``，绝不猜测）。"""

    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()

    except (OSError, ValueError):
        return None



# ============================================================
# 事实视图：task / result / validation（全部只读、确定性）
# ============================================================


def task_view(path: Path, payload: dict[str, Any], raw: bytes) -> dict[str, Any]:
    """task 文件内容身份（原始 bytes SHA-256 + 元数据摘要；**不回显正文**）。"""

    metadata: dict[str, Any] = {
        "task_id": text_value(payload.get("task_id")),
        "title": text_value(payload.get("title")),
        "type": text_value(payload.get("type")),
        "human_gate": text_value(payload.get("human_gate")),
        "depends_on": payload.get("depends_on"),
        "auto_start": payload.get("auto_start"),
        "requires_human_approval": payload.get("requires_human_approval"),
        "max_attempts": payload.get("max_attempts"),
    }

    return {
        "path": str(path),
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
        **metadata,
        "metadata_digest": canonical_digest(metadata),
    }


def result_view(path: Path, payload: dict[str, Any], raw: bytes) -> dict[str, Any]:
    """result 文件内容身份 + 终态判定所需的只读事实。"""

    status = text_value(payload.get("status"))

    normalized = status.lower() if status is not None else None

    attempts = payload.get("attempts")

    attempt_count: int | None = len(attempts) if isinstance(attempts, list) else None

    declared_attempts = payload.get("attempt_count")

    if attempt_count is None and isinstance(declared_attempts, int):
        attempt_count = declared_attempts

    declared_max = payload.get("max_attempts")

    return {
        "path": str(path),
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
        "task_id": text_value(payload.get("task_id")),
        "status": normalized,
        "known_status": normalized is not None and normalized in orch.KNOWN_RESULT_STATUSES,
        "terminal": normalized is not None and normalized in orch.TERMINAL_RESULT_STATUSES,
        "execution_outcome": payload.get("execution_outcome"),
        "normalized_finish_reason": payload.get("normalized_finish_reason"),
        "finished_at": payload.get("finished_at"),
        "attempt_count": attempt_count,
        "max_attempts": declared_max if isinstance(declared_max, int) else None,
    }


def terminal_consistency_issues(payload: dict[str, Any]) -> list[dict[str, str]]:
    """result 终态一致性 fail-closed issue（GOLD-039，只报告，绝不修复）。

    复用 ``orchestrator.result_terminal_consistency`` 的**唯一**判定：只要 result 的
    终态事实自相矛盾（例如顶层 ``status=completed`` 但最终 attempt ``finish_reason=
    aborted``）或落进未知组合，就给出稳定 reason code ⇒ ``facts_complete=false``，
    让 GPT 明确看到「事实不齐」而不是被诱导去猜一个 PASS。

    ``status`` 缺失 / 未知这类事实已由本模块自己的 ``RESULT_STATUS_UNKNOWN``
    fail-closed 表达（见 ``DELEGATED_TERMINAL_CONSISTENCY_CODES``），
    这里不再重复上报同一事实。
    """

    report = terminal_consistency.analyze_result_terminal_consistency(payload)

    return [
        make_issue(
            str(item["code"]),
            f"result 终态事实不自洽（format={report['format']}）：{item['detail']}",
        )
        for item in report["details"]
        if str(item["code"]) not in DELEGATED_TERMINAL_CONSISTENCY_CODES
    ]


def validation_section(payload: dict[str, Any]) -> dict[str, Any]:
    """result 里**已记录**的 validation 摘要（命令 / 返回码 / 超时；**绝不重新执行**）。"""

    attempts = payload.get("attempts")

    results: list[dict[str, Any]] = []

    if isinstance(attempts, list):
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue

            declared_attempt = attempt.get("attempt")

            attempt_number = declared_attempt if isinstance(declared_attempt, int) else index + 1

            validations = attempt.get("validations")

            if not isinstance(validations, list):
                continue

            for validation in validations:
                if not isinstance(validation, dict):
                    continue

                command = text_value(validation.get("command"))

                returncode = validation.get("returncode")

                code_value = returncode if isinstance(returncode, int) else None

                timed_out = validation.get("timed_out") is True

                results.append(
                    {
                        "attempt": attempt_number,
                        "command": command,
                        "returncode": code_value,
                        "timed_out": timed_out,
                        "passed": code_value == 0 and not timed_out,
                    }
                )

    commands = sorted(
        {item["command"] for item in results if isinstance(item["command"], str)}
    )

    if not results:
        status = "not_reported"
    elif all(bool(item["passed"]) for item in results):
        status = "passed"
    else:
        status = "failed"

    return {
        "status": status,
        "source": "result file attempts[].validations (记录值，未重新执行)",
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
        "validation_count": len(results),
        "commands": commands,
        "results": results,
        "digest": canonical_digest(results),
    }



# ============================================================
# 终态 commit 绑定（HEAD 可达历史 ⇒ 内容一致性）
# ============================================================


def empty_section(field_order: tuple[str, ...]) -> dict[str, Any]:
    """按契约字段顺序构造空 section（保证字段顺序稳定、形状恒定）。"""

    return {field: None for field in field_order}


def match_terminal_commit(
    entries: Sequence[dict[str, Any]],
    task_id: str,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """在 git 历史里匹配该 task 的**唯一**终态 commit；歧义一律 fail-closed。"""

    wanted = terminal_commit_subjects(task_id)

    matches = [entry for entry in entries if text_value(entry.get("subject")) in wanted]

    if not matches:
        return None, ISSUE_COMMIT_NOT_FOUND, (
            f"{task_id}: HEAD 可达历史里没有 {' / '.join(wanted)} 记录，无法绑定完成 commit"
        )

    if len(matches) > 1:
        shas = ", ".join(str(entry.get("sha")) for entry in matches)

        return None, ISSUE_COMMIT_AMBIGUOUS, (
            f"{task_id}: 有 {len(matches)} 个终态 commit（{shas}），"
            "歧义必须由 GPT / 人工裁决，绝不猜测"
        )

    return matches[0], None, None


def content_identity(
    *,
    root: Path,
    sha: str | None,
    path: Path | None,
    worktree_raw: bytes | None,
    absent_code: str,
    label: str,
    blob_id_reader: BlobIdReader,
    blob_bytes_reader: BlobBytesReader,
    worktree_blob_id_reader: WorktreeBlobIdReader,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """某文件的内容身份事实：``(facts, issues)``。

    ``facts`` 同时给出两套内容身份口径：

    - ``sha256`` / ``bytes``：**canonical**（目标 commit 里 Git 存储 / GitHub 提供的）字节；
      这是 GPT 写 Review 台账时使用的唯一口径，可由外部独立复算；
    - ``worktree_sha256`` / ``worktree_bytes``：本地工作树的**原始文件字节**（仅本地审计用，
      可能因 ``core.autocrlf`` 与 canonical 不同）；
    - ``worktree_matches_commit``：以 Git 自己的内容口径（blob id）判定「工作树版本 vs
      目标 commit 版本」是否一致；不一致 ⇒ fail-closed。
    """

    facts: dict[str, Any] = {
        "sha256": None,
        "bytes": None,
        "worktree_sha256": sha256_bytes(worktree_raw) if worktree_raw is not None else None,
        "worktree_bytes": len(worktree_raw) if worktree_raw is not None else None,
        "worktree_matches_commit": None,
    }

    if path is None or sha is None:
        return facts, []

    relative = relative_to_root(root, path)

    if relative is None:
        return facts, [
            make_issue(absent_code, f"{label}: {path} 不在仓库根内，无法与 commit {sha} 绑定")
        ]

    blob_id, error = blob_id_reader(root, sha, relative)

    if error == GIT_BLOB_ABSENT:
        return facts, [make_issue(absent_code, f"{label}: commit {sha} 里不存在 {relative}")]

    if error is not None or blob_id is None:
        return facts, [
            make_issue(
                ISSUE_GIT_LOG_UNAVAILABLE,
                f"{label}: 无法读取 commit {sha} 的 {relative} blob id（{error or 'unknown'}）",
            )
        ]

    payload, payload_error = blob_bytes_reader(root, blob_id)

    if payload_error is not None or payload is None:
        return facts, [
            make_issue(
                ISSUE_GIT_LOG_UNAVAILABLE,
                f"{label}: 无法读取 blob {blob_id} 的 canonical 内容"
                f"（{payload_error or 'unknown'}）",
            )
        ]

    facts["sha256"] = sha256_bytes(payload)

    facts["bytes"] = len(payload)

    if worktree_raw is None or not path.is_file():
        return facts, []

    worktree_id, worktree_error = worktree_blob_id_reader(root, relative)

    if worktree_error is not None or worktree_id is None:
        return facts, [
            make_issue(
                ISSUE_GIT_LOG_UNAVAILABLE,
                f"{label}: 无法计算工作树 {relative} 的 blob id（{worktree_error or 'unknown'}）",
            )
        ]

    facts["worktree_matches_commit"] = blob_id == worktree_id

    if facts["worktree_matches_commit"]:
        return facts, []

    return facts, [
        make_issue(
            ISSUE_WORKTREE_COMMIT_MISMATCH,
            f"{label}: 工作树版本与 commit {sha} 不一致（{relative}），内容身份不可信",
        )
    ]



# ============================================================
# 文件事实读取（task / result，只读 + fail-closed）
# ============================================================


def load_task_facts(
    requested: str | None,
    task_path: Path | None,
) -> tuple[dict[str, Any], bytes | None, dict[str, Any] | None, list[dict[str, str]]]:
    """读取 task 文件事实：``(section, raw_bytes, payload, issues)``，全部只读。"""

    section = empty_section(TASK_FIELD_ORDER)

    issues: list[dict[str, str]] = []

    if requested is None or task_path is None:
        return section, None, None, issues

    section["path"] = str(task_path)

    if not task_path.exists():
        issues.append(make_issue(ISSUE_TASK_FILE_MISSING, f"{task_path} 不存在"))

        return section, None, None, issues

    raw, error = read_bytes(task_path)

    if raw is None:
        issues.append(make_issue(ISSUE_TASK_FILE_UNREADABLE, f"{task_path} 不可读: {error}"))

        return section, None, None, issues

    section["worktree_sha256"] = sha256_bytes(raw)

    section["worktree_bytes"] = len(raw)

    payload, parse_error = planner.read_json_mapping(task_path)

    if payload is None:
        issues.append(
            make_issue(
                ISSUE_TASK_FILE_UNREADABLE,
                f"{task_path} JSON 损坏或不是对象: {parse_error}",
            )
        )

        return section, raw, None, issues

    section.update(task_view(task_path, payload, raw))

    declared = text_value(payload.get("task_id"))

    if declared is not None and declared != requested:
        issues.append(
            make_issue(
                ISSUE_TASK_FILE_TASK_ID_MISMATCH,
                f"{task_path}: task 文件 task_id={declared} 与请求的 {requested} 不一致",
            )
        )

    return section, raw, payload, issues


def load_result_facts(
    requested: str | None,
    result_path: Path | None,
) -> tuple[dict[str, Any], bytes | None, dict[str, Any] | None, list[dict[str, str]]]:
    """读取 result 文件事实：``(section, raw_bytes, payload, issues)``，全部只读。"""

    section = empty_section(RESULT_FIELD_ORDER)

    issues: list[dict[str, str]] = []

    if requested is None or result_path is None:
        return section, None, None, issues

    section["path"] = str(result_path)

    if not result_path.exists():
        issues.append(make_issue(ISSUE_RESULT_MISSING, f"{result_path} 不存在"))

        return section, None, None, issues

    raw, error = read_bytes(result_path)

    if raw is None:
        issues.append(make_issue(ISSUE_RESULT_UNREADABLE, f"{result_path} 不可读: {error}"))

        return section, None, None, issues

    section["worktree_sha256"] = sha256_bytes(raw)

    section["worktree_bytes"] = len(raw)

    payload, parse_error = planner.read_json_mapping(result_path)

    if payload is None:
        issues.append(
            make_issue(
                ISSUE_RESULT_UNREADABLE,
                f"{result_path} JSON 损坏或不是对象: {parse_error}",
            )
        )

        return section, raw, None, issues

    section.update(result_view(result_path, payload, raw))

    # GOLD-039：终态一致性事实（只报告）。矛盾 ⇒ 稳定 reason code ⇒
    # binding.facts_complete=false，review / backlog 自动 fail-closed。
    issues.extend(terminal_consistency_issues(payload))

    declared = text_value(payload.get("task_id"))

    if declared is not None and declared != requested:
        issues.append(
            make_issue(
                ISSUE_RESULT_TASK_ID_MISMATCH,
                f"{result_path}: result 文件 task_id={declared} 与请求的 {requested} 不一致",
            )
        )

    status = section["status"]

    if status is None:
        issues.append(make_issue(ISSUE_RESULT_STATUS_UNKNOWN, f"{result_path}: 缺少 status 字段"))

    elif status not in orch.KNOWN_RESULT_STATUSES:
        issues.append(
            make_issue(
                ISSUE_RESULT_STATUS_UNKNOWN,
                f"{result_path}: status={status} 不在已知词表 {sorted(orch.KNOWN_RESULT_STATUSES)}",
            )
        )

    elif status not in orch.TERMINAL_RESULT_STATUSES:
        issues.append(
            make_issue(
                ISSUE_RESULT_NOT_TERMINAL,
                f"{result_path}: status={status} 不是终态"
                f"（{TERMINAL_STATUS_HINT}），无法绑定完成 commit",
            )
        )

    return section, raw, payload, issues



def resolve_commit_binding(
    *,
    requested: str | None,
    root: Path,
    git_view: dict[str, Any],
    task_path: Path | None,
    task_raw: bytes | None,
    result_path: Path | None,
    result_raw: bytes | None,
    commit_log_provider: CommitLogProvider,
    blob_id_reader: BlobIdReader,
    blob_bytes_reader: BlobBytesReader,
    worktree_blob_id_reader: WorktreeBlobIdReader,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, str]]]:
    """解析终态完成 commit，并把 task / result 内容身份绑定到该 commit（只读、fail-closed）。

    返回 ``(commit_section, {"result": facts, "task": facts}, issues)``；
    ``facts`` 是 :func:`content_identity` 的 canonical + worktree 内容身份事实。
    """

    section = empty_section(COMMIT_FIELD_ORDER)

    content_facts: dict[str, dict[str, Any]] = {}

    issues: list[dict[str, str]] = []

    section["resolved"] = False

    section["branch"] = git_view.get("branch")

    section["head"] = git_view.get("head")

    section["history_scope"] = (
        "HEAD-reachable git log; subject match 'ai: complete/blocked <task_id>'"
    )

    if requested is None:
        return section, content_facts, issues

    entries, log_error = commit_log_provider(root)

    if log_error is not None or entries is None:
        issues.append(
            make_issue(ISSUE_GIT_LOG_UNAVAILABLE, f"无法读取 git 历史: {log_error or 'empty'}")
        )

        section["reason_code"] = ISSUE_GIT_LOG_UNAVAILABLE

        return section, content_facts, issues

    entry, code, detail = match_terminal_commit(entries, requested)

    if code is not None or entry is None:
        issues.append(make_issue(code or ISSUE_COMMIT_NOT_FOUND, str(detail)))

        section["reason_code"] = code or ISSUE_COMMIT_NOT_FOUND

        return section, content_facts, issues

    sha = text_value(entry.get("sha"))

    if sha is None or FULL_COMMIT_SHA_PATTERN.match(sha) is None:
        issues.append(
            make_issue(
                ISSUE_COMMIT_SHA_INVALID,
                f"{requested}: 终态 commit sha 非法: {entry.get('sha')!r}",
            )
        )

        section["reason_code"] = ISSUE_COMMIT_SHA_INVALID

        return section, content_facts, issues

    section["resolved"] = True

    section["sha"] = sha

    section["subject"] = text_value(entry.get("subject"))

    section["committed_at"] = text_value(entry.get("committed_at"))

    targets = (
        ("result", result_path, result_raw, ISSUE_RESULT_NOT_IN_COMMIT),
        ("task", task_path, task_raw, ISSUE_TASK_NOT_IN_COMMIT),
    )

    for label, path, worktree_raw, absent_code in targets:
        facts, blob_issues = content_identity(
            root=root,
            sha=sha,
            path=path,
            worktree_raw=worktree_raw,
            absent_code=absent_code,
            label=f"{requested} {label}",
            blob_id_reader=blob_id_reader,
            blob_bytes_reader=blob_bytes_reader,
            worktree_blob_id_reader=worktree_blob_id_reader,
        )

        content_facts[label] = facts

        issues.extend(blob_issues)

    return section, content_facts, issues



# ============================================================
# manifest 组装（只读事实；绝不签发 Review 结论）
# ============================================================


def authority_section() -> dict[str, Any]:
    """本工具的**职责边界**（机器可读契约：只产事实，不产结论、不写状态）。"""

    return {
        "schema": REVIEW_BINDING_AUTHORITY_SCHEMA,
        "read_only": True,
        "emits_review_outcome": False,
        "tool_can_sign_review": False,
        "tool_can_advance_state": False,
        "tool_can_qualify_data": False,
        "tool_can_cross_human_gate": False,
        "writes_review_ledger": False,
        "writes_project_state": False,
        "writes_tasks": False,
        "writes_results": False,
        "review_authority": "gpt_only",
        "writer_agents": list(planner.PLANNER_AGENTS),
        "reader_agents": list(planner.EXECUTOR_AGENTS),
        "fact_scope": "content identity only: sha256 of raw file bytes + git identity",
        "facts_complete_semantics": "事实齐全供 GPT Review 使用，不是 Review 结论",
        "blocking_human_gates": sorted(orch.BLOCKING_HUMAN_GATES),
    }


def determinism_section() -> dict[str, Any]:
    """确定性契约的机器可读声明（只有规则，不含任何状态事实）。"""

    return {
        "digest_field": "facts_digest",
        "digest_algorithm": "sha256(canonical json: sort_keys + compact separators)",
        "excluded_from_facts": list(FACTS_EXCLUDED_KEYS),
        "wall_clock_in_facts": False,
        "mtime_used_as_identity": False,
        "model_output_used_as_identity": False,
        "content_identity_unit": "canonical: git blob bytes; worktree: raw file bytes",
        "field_order": list(MANIFEST_FIELD_ORDER),
        "ordering": dict(MANIFEST_ORDERING),
    }


def manifest_facts(manifest: dict[str, Any]) -> dict[str, Any]:
    """剔除 wall-clock / 自引用字段后的**确定性事实**视图。"""

    return {key: value for key, value in manifest.items() if key not in FACTS_EXCLUDED_KEYS}


def manifest_facts_digest(manifest: dict[str, Any]) -> str:
    """确定性事实的 sha256：相同输入 ⇒ 相同 digest（幂等、无自引用）。"""

    return canonical_digest(manifest_facts(manifest))


def manifest_exit_code(issues: Sequence[dict[str, str]]) -> int:
    """退出码：``0`` 事实齐全 / ``2`` fail-closed（缺事实 / 漂移 / 身份不可解析）。"""

    return EXIT_DRIFT if issues else EXIT_OK



def build_review_binding_manifest(
    task_id: str,
    *,
    root: Path | None = None,
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    generated_at: str | None = None,
    commit_log_provider: CommitLogProvider | None = None,
    blob_id_reader: BlobIdReader | None = None,
    blob_bytes_reader: BlobBytesReader | None = None,
    worktree_blob_id_reader: WorktreeBlobIdReader | None = None,
) -> dict[str, Any]:
    """构建只读 Review Binding Manifest（**只产事实，绝不签发 Review 结论**）。"""

    resolved_root = Path(root) if root is not None else ROOT

    resolved_tasks = Path(tasks_dir) if tasks_dir is not None else Path(orch.TASK_DIR)

    resolved_results = Path(results_dir) if results_dir is not None else Path(orch.RESULT_DIR)

    log_provider = commit_log_provider if commit_log_provider is not None else git_commit_log

    read_blob_id = blob_id_reader if blob_id_reader is not None else git_blob_id

    read_blob_content = blob_bytes_reader if blob_bytes_reader is not None else git_blob_content

    read_worktree_blob_id = (
        worktree_blob_id_reader if worktree_blob_id_reader is not None else git_worktree_blob_id
    )

    requested = text_value(task_id)

    issues: list[dict[str, str]] = []

    if requested is None or TASK_ID_PATTERN.match(requested) is None:
        issues.append(
            make_issue(
                ISSUE_TASK_ID_INVALID,
                f"task_id 非法: {task_id!r}（只接受非空、无路径分隔符的 task id）",
            )
        )

        requested = None

    task_path = (resolved_tasks / f"{requested}.json") if requested is not None else None

    result_path = (resolved_results / f"{requested}.json") if requested is not None else None

    task_section, task_raw, task_payload, task_issues = load_task_facts(requested, task_path)

    result_section, result_raw, result_payload, result_issues = load_result_facts(
        requested, result_path
    )

    issues.extend(task_issues)

    issues.extend(result_issues)

    git_view, git_issues = planner.git_section(resolved_root)

    issues.extend(git_issues)

    commit_section, content_facts, commit_issues = resolve_commit_binding(
        requested=requested,
        root=resolved_root,
        git_view=git_view,
        task_path=task_path,
        task_raw=task_raw,
        result_path=result_path,
        result_raw=result_raw,
        commit_log_provider=log_provider,
        blob_id_reader=read_blob_id,
        blob_bytes_reader=read_blob_content,
        worktree_blob_id_reader=read_worktree_blob_id,
    )

    issues.extend(commit_issues)

    for label, section in (("task", task_section), ("result", result_section)):
        facts = content_facts.get(label)

        if facts is not None:
            section.update(facts)

    validation = validation_section(result_payload if result_payload is not None else {})

    gate = text_value(task_payload.get("human_gate")) if task_payload is not None else None

    requires_approval = (
        task_payload is not None and task_payload.get("requires_human_approval") is True
    )

    ordered_issues = sorted(issues, key=lambda item: (item["code"], item["detail"]))

    reason_codes = sorted({issue["code"] for issue in ordered_issues})

    manifest: dict[str, Any] = {
        "schema": REVIEW_BINDING_SCHEMA,
        "schema_version": REVIEW_BINDING_SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else orch.now_iso(),
        "read_only": True,
        "task_id": requested,
        "paths": {
            "root": str(resolved_root),
            "tasks_dir": str(resolved_tasks),
            "results_dir": str(resolved_results),
            "task_file": str(task_path) if task_path is not None else None,
            "result_file": str(result_path) if result_path is not None else None,
        },
        "task": task_section,
        "result": result_section,
        "commit": commit_section,
        "validation": validation,
        "binding": {
            "facts_complete": not reason_codes,
            "reason_codes": reason_codes,
            "human_gate": gate,
            "blocking_human_gate": bool(gate) and gate in orch.BLOCKING_HUMAN_GATES,
            "requires_human_approval": requires_approval,
        },
        "authority": authority_section(),
        "determinism": determinism_section(),
        "issues": ordered_issues,
        "summary": {
            "issue_count": len(ordered_issues),
            "error_count": len(ordered_issues),
            "warning_count": 0,
            "facts_complete": not reason_codes,
            "exit_code": manifest_exit_code(ordered_issues),
        },
    }

    manifest["facts_digest"] = manifest_facts_digest(manifest)

    return manifest


def render_review_binding_manifest(manifest: dict[str, Any], *, ensure_ascii: bool = True) -> str:
    """确定性 JSON 渲染（字段顺序固定 + 缩进固定；默认纯 ASCII，机器通道安全）。"""

    return json.dumps(manifest, ensure_ascii=ensure_ascii, indent=2, sort_keys=False) + "\n"



# ============================================================
# 只读 CLI（python -m orchestrator.review_binding）
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.review_binding",
        description=(
            "GPT Review Binding Manifest（GOLD-031）：给定 task_id 只读输出 "
            "result SHA-256 / 完成 commit identity / task 内容身份 / validation 摘要；"
            "绝不签发 Review 结论、绝不修改任何项目状态。"
        ),
    )

    parser.add_argument(
        "--task",
        required=True,
        help="要绑定的 task_id（例如 GOLD-028）",
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="仓库根目录（默认：本模块所在项目根）",
    )

    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=None,
        help="tasks 目录（默认 <root>/.ai/tasks）",
    )

    parser.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="results 目录（默认 <root>/.ai/results）",
    )

    parser.add_argument(
        "--generated-at",
        default=None,
        help="固定 generated_at（便于审计与字节级复现；默认取当前时间）",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """只读 CLI：manifest 写 stdout（纯 ASCII JSON），issue 摘要写 stderr。"""

    args = build_parser().parse_args(argv)

    root = Path(args.root) if args.root is not None else ROOT

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir is not None else root / ".ai" / "tasks"

    results_dir = (
        Path(args.results_dir) if args.results_dir is not None else root / ".ai" / "results"
    )

    manifest = build_review_binding_manifest(
        args.task,
        root=root,
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        generated_at=args.generated_at,
    )

    sys.stdout.write(render_review_binding_manifest(manifest))

    sys.stdout.flush()

    for issue in manifest["issues"]:
        print(f"[{issue['severity']}] {issue['code']}: {issue['detail']}", file=sys.stderr)

    if not manifest["binding"]["facts_complete"]:
        codes = ", ".join(manifest["binding"]["reason_codes"])

        print(f"[binding] facts_complete=False | fail-closed: {codes}", file=sys.stderr)

    return int(manifest["summary"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
