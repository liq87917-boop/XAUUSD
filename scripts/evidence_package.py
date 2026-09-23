"""Evidence 本地 Package / Manifest Builder CLI（GOLD-017，纯本地 / 默认 dry-run）。

把**人工显式提供**的授权 / 时间 / availability / OOS 元数据与**人工指定的现有 evidence 文件**
整理成 GOLD-011 inbox 候选包内可直接消费的 ``manifest.json``（含真实字节 SHA-256），
从而消除“手工写 manifest、手工算摘要”的交付摩擦。

用法::

    # ① 默认 dry-run：只打印确定性、脱敏的 manifest 预览与逐行预检（**零写入**）
    python -m scripts.evidence_package \\
        --package-dir logs/evidence/inbox/vendor-author-2026-06 \\
        --file author-2026-06.jsonl \\
        --evidence-type author --source vendor-author \\
        --authorization-reference https://vendor.example/terms \\
        --time-semantics provider_export_iso8601_with_tz \\
        --availability-semantics provider_archive_export_daily_snapshot \\
        --historical-oos-applicable true --json

    # ② 唯一写开关：--out 必须**正好**是 <package-dir>/manifest.json（原子写 + 单实例锁）
    python -m scripts.evidence_package ... \\
        --out logs/evidence/inbox/vendor-author-2026-06/manifest.json

    # ③ 端到端：写完后交给**真实** GOLD-011 inbox scanner 做只读预检
    python -m scripts.evidence_inbox --inbox-dir logs/evidence/inbox --json

    # ④ 固定审计时点（ISO8601 **必须带时区**；用于人工复现，不改变任何口径）
    python -m scripts.evidence_package ... --as-of 2026-09-23T00:00:00+00:00

安全与边界：

- **显式人工输入**：``--evidence-type`` / ``--source`` / ``--authorization-reference`` /
  ``--time-semantics`` / ``--availability-semantics`` / ``--historical-oos-applicable`` 与
  ``--file`` 清单**必须**由人工给出；工具**绝不**从文件名 / 正文 / URL / mtime / 当前时间推断或
  补造授权、发布时间、采集时间、availability、OOS 语义；
- **只读原始 evidence**：只读取显式给出的、已存在于 package 目录内的**单级常规文件**；
  拒绝绝对路径 / ``..`` / 多级路径 / 符号链接 / 目录 / ``manifest.json`` 自引用 / 未支持扩展 /
  重复或大小写冲突路径 / 包外文件 / 包内未声明文件；**绝不**移动 / 删除 / 改名 / 改写任何文件；
- **默认零写入**：不传 ``--out`` 时只打印 stdout（不取锁、不写任何文件）；``--out`` 必须**正好**是
  目标 package 目录内的固定 ``manifest.json``，否则退出码 ``3``、零写入；
- **不静默覆盖**：既有 manifest 与本次规范化内容**逐字节一致** → 幂等成功；**内容不同** →
  ``MANIFEST_CONFLICT``（退出码 ``5``、零写入）；本命令**没有** ``--force`` / ``--overwrite``，
  更正必须新建 package 目录 / 新内容身份；
- **写入前同源预检**：写 manifest 前复用 ``evidence-intake-v1`` 的 ``read_input_file`` +
  ``assess_row`` 做只读逐行预检，输出 ``accepted`` / ``quarantined`` / ``not_oos_eligible`` 与稳定
  原因码；预检**不写入** evidence 原始行，也**不表示**授权已人工核验 / **不表示**数据资格通过；
- **不解除 blocker**：``blocker_active`` / ``human_gate_required`` / ``requires_human_action`` 恒为
  true，``data_qualification_passed`` / ``phase_transition_allowed`` / ``auto_intake_allowed`` /
  ``writes_database`` 恒为 false（**硬编码**）；``PHASE3_3_DATA`` **保持 BLOCKED**，
  Phase 切换仍须 ``.ai/DEVELOPMENT_PROTOCOL.md`` 的 **L3 人工确认**；
- **凭据与脱敏**：``authorization_reference`` / ``source`` / ``notes`` 走既有
  :mod:`src.common.redaction` 与 ``valid_reference``；敏感键名 / 疑似凭据 blob 一律**拒绝**；
  错误输出统一脱敏，``--json`` 时 stdout 保持纯 JSON（提示信息走 stderr）；
- 退出码：``0`` 预览已生成 / manifest 已写入或幂等未变（**仍不是**资格通过）/ ``2`` 参数错误
  （缺必填元数据 / 时区缺失 / 非法取值）/ ``3`` package 目录或输出路径不可用（含 ``--out`` 不是固定
  ``manifest.json``）/ ``4`` 输入未通过 fail-closed 校验（路径不安全 / 缺失 / 重复 / 未声明 /
  敏感值 / 未支持格式 / 示例名称）/ ``5`` 既有 manifest 与本次内容不同（``MANIFEST_CONFLICT``）/
  ``6`` 锁冲突。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - editable 安装时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

from scripts._console import configure_stdout, safe_print  # noqa: E402
from scripts.evidence_readiness import parse_as_of  # noqa: E402
from src.common.redaction import safe_text  # noqa: E402
from src.evidence import (  # noqa: E402
    EVIDENCE_TYPES,
    EXIT_OK,
    MANIFEST_FILE_NAME,
    MAX_FILE_COUNT,
    LockConflictError,
    LockUnavailableError,
    package_builder_exit_code_for,
    render_package_summary,
    run_package_builder,
)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（元数据与文件清单**必须**显式给出；默认 dry-run，零写入）。"""
    parser = argparse.ArgumentParser(
        prog="evidence_package",
        description=(
            "Evidence 本地 Package / Manifest Builder：把人工显式提供的授权 / 时间 / "
            "OOS 元数据与人工指定的现有 evidence 文件整理成 GOLD-011 inbox 可直接消费的 "
            "manifest.json（真实字节 SHA-256 + 确定性排序）；默认 dry-run，只有显式 --out 才写"
            f" <package-dir>/{MANIFEST_FILE_NAME}；不联网、不写数据库、不自动 intake、"
            "不解除 PHASE3_3_DATA。"
        ),
    )
    parser.add_argument(
        "--package-dir",
        type=Path,
        required=True,
        help="**必填**：候选包目录（必须已存在；manifest.json 只能写在该目录内）",
    )
    parser.add_argument(
        "--file",
        dest="files",
        action="append",
        required=True,
        metavar="RELATIVE_NAME",
        help=(
            "**必填**（可重复）：要纳入 package 的现有 evidence 文件（目录内的**单级**文件名；"
            f"最多 {MAX_FILE_COUNT} 个；本工具不自动扫描目录）"
        ),
    )
    parser.add_argument(
        "--evidence-type",
        choices=EVIDENCE_TYPES,
        required=True,
        help="**必填**：证据类别（author / news；大小写敏感，不做静默归一）",
    )
    parser.add_argument("--source", required=True, help="**必填**：来源名（人工显式提供）")
    parser.add_argument(
        "--authorization-reference",
        required=True,
        help="**必填**：授权引用（https URL 或 docs/legal/ 内路径；人工显式提供，不推断）",
    )
    parser.add_argument(
        "--time-semantics",
        required=True,
        help="**必填**：时间语义说明（人工显式提供；工具绝不推断 / 不伪造证据时间）",
    )
    parser.add_argument(
        "--availability-semantics",
        required=True,
        help="**必填**：可用性（availability）语义说明（人工显式提供）",
    )
    parser.add_argument(
        "--historical-oos-applicable",
        choices=("true", "false"),
        required=True,
        help="**必填**：是否适用于历史 OOS（人工显式声明；false 时 scanner 会整包隔离）",
    )
    parser.add_argument("--notes", default=None, help="可选：人工备注（会被脱敏并限长）")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "manifest 落盘路径（**唯一写开关**；必须**正好**是 <package-dir>/manifest.json；"
            "原子写 + 单实例锁；内容不同则 fail-closed）"
        ),
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=None,
        help="单实例锁文件路径（缺省 = 同级 <package-dir>.manifest.lock；防并发写同一 package）",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="审计时点（ISO8601，必须带时区；缺省 = 当前 UTC 时间；只是审计操作时间）",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="输出稳定机器可读 JSON（默认输出人类可读 Markdown）",
    )
    return parser


def _notify(message: str, *, json_output: bool) -> None:
    """提示信息：``--json`` 时走 stderr，保证 stdout 是纯 JSON。"""
    if json_output:
        print(safe_text(message), file=sys.stderr)
    else:
        safe_print(message)


def main(
    argv: Sequence[str] | None = None,
    *,
    moment: datetime | None = None,
) -> int:
    """CLI 主入口：执行**一次** manifest 构建，返回进程退出码（零网络、零数据库）。"""
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    run_moment = moment if moment is not None else (as_of or datetime.now(UTC))
    json_output = bool(args.json_output)
    try:
        preview = run_package_builder(
            args.package_dir,
            files=tuple(args.files),
            evidence_type=args.evidence_type,
            source=args.source,
            authorization_reference=args.authorization_reference,
            time_semantics=args.time_semantics,
            availability_semantics=args.availability_semantics,
            historical_oos_applicable=args.historical_oos_applicable == "true",
            moment=run_moment,
            notes=args.notes,
            out_path=args.out,
            lock_path=args.lock,
        )
    except (RuntimeError, LockConflictError, LockUnavailableError) as exc:
        # fail-closed：任何失败都**不写**任何文件；错误信息统一脱敏
        print(
            f"[evidence] manifest 未生成（fail-closed，未写入任何文件）：{safe_text(str(exc))}",
            file=sys.stderr,
        )
        return package_builder_exit_code_for(exc)
    text = (
        json.dumps(preview.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if json_output
        else render_package_summary(preview)
    )
    safe_print(text)
    if args.out is not None:
        _notify(
            f"[evidence] manifest 已处理：{args.out}（{preview.write_status.value}）"
            "（下一步：交给 scripts/evidence_inbox --inbox-dir 做只读预检；"
            "人工确认后仍须显式 evidence_operator workflow --no-dry-run）",
            json_output=json_output,
        )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    raise SystemExit(main())
