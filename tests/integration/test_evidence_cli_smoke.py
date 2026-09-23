"""Evidence 系列 CLI 的轻量 import / ``--help`` 冒烟门禁（GOLD-018）。

为什么需要这一层：这些入口都以 ``python -m scripts.evidence_xxx`` 形式发布，导入期会链式
import ``src.monitoring`` / ``src.evidence``。GOLD-018 之前，一旦进程先 import 了
``src.monitoring``，再进入 ``src.evidence`` 链就会因包初始化期循环导入直接 ImportError
（连 ``--help`` 都打不开）。本门禁把"任意 CLI 都能在**全新子进程**里导入 + ``--help`` 可用"
钉死，并且：

- **零网络 / 零数据库**：只跑模块 import 与 ``--help``（argparse 在解析阶段退出，不执行任何动作，
  不建连接、不连真实库）；
- **零文件写入**：子进程的 cwd 指向**空的** ``tmp_path``（项目以 ``PYTHONPATH`` 注入），跑完断言
  ``tmp_path`` 仍为空 —— 任何相对路径写入都会立刻让用例失败；
- **覆盖守门**：``scripts/evidence_*.py`` 新增入口必须同步登记，否则用例失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SENTINEL = "GOLD018_CLI_OK"

#: 证据链 CLI 入口（``scripts/evidence_*.py`` + 单步 intake 入口）。
EVIDENCE_CLI_MODULES: tuple[str, ...] = (
    "scripts.evidence_decision_packet",
    "scripts.evidence_decision_record",
    "scripts.evidence_gap_diagnostic",
    "scripts.evidence_handoff",
    "scripts.evidence_human_verification_attestation",
    "scripts.evidence_inbox",
    "scripts.evidence_intake_handoff",
    "scripts.evidence_intake_plan",
    "scripts.evidence_intake_receipt",
    "scripts.evidence_operator",
    "scripts.evidence_package",
    "scripts.evidence_readiness",
    "scripts.evidence_readiness_runner",
    "scripts.evidence_readiness_watch",
    "scripts.evidence_review",
    "scripts.intake_evidence",
)

#: 观测层 CLI：与证据链共用 ``src.monitoring`` ↔ ``src.evidence`` 导入链，同样纳入冒烟。
MONITORING_CLI_MODULES: tuple[str, ...] = ("scripts.report_collector_health",)

ALL_CLI_MODULES: tuple[str, ...] = (*EVIDENCE_CLI_MODULES, *MONITORING_CLI_MODULES)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在**空的临时目录**里以真实进程运行（项目通过 ``PYTHONPATH`` 注入，cwd 保持干净）。"""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def _assert_no_side_effects(cwd: Path) -> None:
    """import / ``--help`` 都不得写文件（相对路径写入会落在临时 cwd 并被这里抓住）。"""
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("module", ALL_CLI_MODULES)
def test_cli_module_imports_in_fresh_process(module: str, tmp_path: Path) -> None:
    completed = _run(
        tmp_path,
        "-c",
        f"import importlib; importlib.import_module('{module}'); print('{SENTINEL}')",
    )

    assert completed.returncode == 0, completed.stderr
    assert SENTINEL in completed.stdout
    assert "ImportError" not in completed.stderr
    assert "circular import" not in completed.stderr
    _assert_no_side_effects(tmp_path)


@pytest.mark.parametrize("module", ALL_CLI_MODULES)
def test_cli_help_smoke_in_fresh_process(module: str, tmp_path: Path) -> None:
    completed = _run(tmp_path, "-m", module, "--help")

    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.lower()
    assert "Traceback" not in completed.stderr
    _assert_no_side_effects(tmp_path)


def test_every_evidence_script_is_covered() -> None:
    """覆盖守门：``scripts/evidence_*.py`` 新增入口必须同步登记（否则本用例失败）。"""
    on_disk = {path.stem for path in SCRIPTS_DIR.glob("evidence_*.py")}
    covered = {module.rsplit(".", 1)[1] for module in EVIDENCE_CLI_MODULES}

    assert on_disk - covered == set(), sorted(on_disk - covered)
    assert "intake_evidence" in covered
