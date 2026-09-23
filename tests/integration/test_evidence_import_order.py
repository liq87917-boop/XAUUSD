"""monitoring ↔ evidence 包边界的**导入顺序**回归门禁（GOLD-018）。

为什么必须是**全新子进程**：进程内 ``sys.modules`` 缓存会掩盖"先导入谁"的差异，而本次故障
恰恰只在特定顺序下出现（GOLD-017 任务外发现，并在纯净 HEAD 复现）::

    monitoring-first : ImportError: cannot import name 'BatchQuantification' from
                       partially initialized module 'src.monitoring.evidence_readiness'
                       (most likely due to a circular import)
    evidence-first   : OK

根因：``src.evidence.__init__`` 在包初始化阶段 eager import 整个依赖图
（``decision_packet`` → ``readiness_runner`` → ``handoff``），而 ``handoff`` 又 import
``src.monitoring.evidence_readiness``；同时 ``src.monitoring.__init__`` eager import
``evidence_readiness``（→ ``src.evidence.contracts``）。修复方向是**包边界治理**
（PEP 562 惰性导出），**不是**改业务逻辑、阈值或安全语义。

本文件锁定：monitoring-first / evidence-first / 直接子模块 first / from-import / star-import /
重复导入等各种顺序在 fresh subprocess 里都必须成功，且不得出现上述循环导入签名；
``PHASE3_3_DATA`` 仍保持 BLOCKED。全部零网络 / 零数据库 / 零文件写入。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "GOLD018_IMPORT_OK"

#: 修复前的失败签名：任何一条重新出现都说明包边界退化。
CIRCULAR_IMPORT_SIGNATURES: tuple[str, ...] = (
    "cannot import name 'BatchQuantification' from partially initialized module",
    "most likely due to a circular import",
)

MONITORING_FIRST: tuple[str, ...] = (
    "import src.monitoring",
    "import src.evidence",
    "import src.evidence.decision_packet",
    "import src.evidence.handoff",
    "import src.evidence.readiness_runner",
)

EVIDENCE_FIRST: tuple[str, ...] = (
    "import src.evidence.readiness_runner",
    "import src.evidence.handoff",
    "import src.evidence.decision_packet",
    "import src.evidence",
    "import src.monitoring",
)

#: ``(用例名, 导入语句序列)``：每条语句在 fresh subprocess 里按顺序执行。
IMPORT_ORDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("monitoring-first", MONITORING_FIRST),
    ("evidence-first", EVIDENCE_FIRST),
    (
        "evidence-handoff-first",
        (
            "import src.evidence.handoff",
            "import src.monitoring",
            "import src.monitoring.evidence_readiness",
        ),
    ),
    (
        "monitoring-evidence-readiness-first",
        (
            "import src.monitoring.evidence_readiness",
            "import src.evidence",
            "import src.evidence.decision_packet",
        ),
    ),
    (
        "decision-packet-first",
        (
            "import src.evidence.decision_packet",
            "import src.monitoring",
            "import src.evidence.readiness_runner",
        ),
    ),
    (
        "from-import-monitoring-first",
        (
            "from src.monitoring import PHASE3_3_BLOCKER_CODE, build_readiness_report",
            "from src.evidence import EVIDENCE_CONTRACT_VERSION, EvidenceIntakeReport",
            "from src.evidence.handoff import build_handoff_report",
            "from src.monitoring import BatchQuantification",
        ),
    ),
    (
        "from-import-evidence-first",
        (
            "from src.evidence import EVIDENCE_CONTRACT_VERSION, EvidenceIntakeReport",
            "from src.evidence.handoff import build_handoff_report",
            "from src.monitoring import BatchQuantification, PHASE3_3_BLOCKER_CODE",
        ),
    ),
    (
        "star-import-then-monitoring",
        (
            "from src.evidence import *",
            "import src.monitoring",
        ),
    ),
    (
        "repeated-and-interleaved-imports",
        (
            "from importlib import import_module as im",
            "import src.monitoring",
            "import src.evidence",
            "import src.monitoring",
            "import src.evidence",
            "import src.monitoring.evidence_readiness",
            "assert im('src.monitoring') is im('src.monitoring')",
            "assert im('src.evidence.handoff') is im('src.evidence.handoff')",
            "assert im('src.monitoring.evidence_readiness') is im("
            "'src.monitoring.evidence_readiness')",
        ),
    ),
)


def _run(statements: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """在 fresh subprocess 里按顺序执行导入语句（成功时打印哨兵）。"""
    source = "; ".join((*statements, f"print('{SENTINEL}')"))
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize(
    ("order", "statements"), IMPORT_ORDERS, ids=[name for name, _ in IMPORT_ORDERS]
)
def test_import_order_is_stable(order: str, statements: tuple[str, ...]) -> None:
    completed = _run(statements)

    assert completed.returncode == 0, f"[{order}] {completed.stderr}"
    assert SENTINEL in completed.stdout, f"[{order}] stdout={completed.stdout!r}"
    for signature in CIRCULAR_IMPORT_SIGNATURES:
        assert signature not in completed.stderr, f"[{order}] 循环导入签名重现：{signature}"


def test_evidence_package_init_does_not_import_monitoring() -> None:
    """根因门禁：``import src.evidence`` 不得拉入 ``src.monitoring``（包初始化不加载依赖图）。"""
    completed = _run(
        (
            "import sys",
            "import src.evidence",
            "assert 'src.monitoring' not in sys.modules, "
            "'src.evidence 包初始化 eager import 了 src.monitoring'",
        )
    )

    assert completed.returncode == 0, completed.stderr
    assert SENTINEL in completed.stdout


def test_monitoring_package_init_does_not_import_evidence_chain() -> None:
    """根因门禁：``import src.monitoring`` 不得 eager 拉入 ``src.evidence`` 的重型子模块。

    ``handoff`` / ``decision_packet`` 正是旧版循环导入的"铰链"：它们既被 ``src.evidence.__init__``
    eager 拉入，又反向 import ``src.monitoring.evidence_readiness``。
    """
    completed = _run(
        (
            "import sys",
            "import src.monitoring",
            "assert 'src.evidence.handoff' not in sys.modules, "
            "'src.monitoring 包初始化 eager import 了 src.evidence.handoff'",
            "assert 'src.evidence.decision_packet' not in sys.modules, "
            "'src.monitoring 包初始化 eager import 了 src.evidence.decision_packet'",
        )
    )

    assert completed.returncode == 0, completed.stderr
    assert SENTINEL in completed.stdout


def test_public_api_and_blocker_hold_after_monitoring_first_import() -> None:
    """顺序修复**不得**改变对外语义：公开名仍指向同一对象，``PHASE3_3_DATA`` 仍 BLOCKED。"""
    completed = _run(
        (
            "import src.monitoring",
            "import src.evidence",
            "from src.monitoring import BatchQuantification, PHASE3_3_BLOCKER_CODE",
            "from src.evidence import EvidenceIntakeReport, decision_packet, handoff",
            "assert PHASE3_3_BLOCKER_CODE == 'PHASE3_3_DATA', PHASE3_3_BLOCKER_CODE",
            "assert BatchQuantification is src.monitoring.evidence_readiness.BatchQuantification",
            "assert handoff is src.evidence.handoff",
            "assert decision_packet is src.evidence.decision_packet",
            "assert EvidenceIntakeReport.__name__ == 'EvidenceIntakeReport'",
        )
    )

    assert completed.returncode == 0, completed.stderr
    assert SENTINEL in completed.stdout
