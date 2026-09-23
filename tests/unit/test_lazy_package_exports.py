"""包边界惰性导出（PEP 562）一致性门禁（GOLD-018；纯函数，零数据库 / 零网络 / 零写入）。

背景（真实故障，GOLD-017 任务外发现并在纯净 HEAD 复现）::

    .venv\\Scripts\\python.exe -c "import src.monitoring; import src.evidence"
    ImportError: cannot import name 'BatchQuantification' from partially initialized module
    'src.monitoring.evidence_readiness' (most likely due to a circular import)

根因是**包边界**问题而非业务逻辑问题：``src.evidence.__init__`` 在包初始化阶段 eager import
整个依赖图（``decision_packet`` → ``readiness_runner`` → ``handoff``），而 ``handoff`` 又 import
``src.monitoring.evidence_readiness``；同时 ``src.monitoring.__init__`` 也 eager import
``evidence_readiness``（→ ``src.evidence.contracts``）。两条链在"先导入谁"不同时会踩到**尚未
初始化完**的模块，于是 monitoring-first 直接 ImportError、evidence-first 却正常。

本文件锁定修复后的**包边界契约**（不触碰任何资格算法 / 阈值 / 安全语义）：

1. ``__all__`` 与惰性导出表**同源**：既不漏登记，也不登记取不到的名字；
2. 每个公开名的取值必须 **is** 其定义子模块上的对象（别名同理）——**禁止复制第二份事实源**
   （不复制类 / 阈值 / 枚举 / 常量）；
3. 两个包的 ``__init__`` 在模块级**只允许** ``__future__`` / ``importlib`` / ``typing``：源码级
   守卫，谁把 eager 子模块导入写回来，本用例立刻失败；
4. 既有用法保持兼容：``from src.<pkg> import X``、``from src.<pkg> import <子模块>``、
   ``import src.<pkg>`` 后再取属性；
5. 未登记的名字必须抛 ``AttributeError``（绝不静默吞掉 ImportError / 返回 None）。

真实"导入顺序"验证（fresh subprocess）见 ``tests/integration/test_evidence_import_order.py``；
CLI 冒烟见 ``tests/integration/test_evidence_cli_smoke.py``。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import src.evidence
import src.monitoring

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES: tuple[str, ...] = ("src.evidence", "src.monitoring")
PACKAGE_INITS: tuple[Path, ...] = (
    REPO_ROOT / "src" / "evidence" / "__init__.py",
    REPO_ROOT / "src" / "monitoring" / "__init__.py",
)
#: 包 ``__init__`` 模块级唯一允许的导入来源（惰性导出的全部依赖）。
ALLOWED_INIT_IMPORTS: frozenset[str] = frozenset({"__future__", "importlib", "typing"})

#: 既有公开导出见证（GOLD-005 ~ GOLD-017）：修复**不得**以删 API 为代价。
EVIDENCE_PUBLIC_WITNESS: tuple[str, ...] = (
    "EVIDENCE_CONTRACT_VERSION",
    "EvidenceIntakeReport",
    "EvidenceLedger",
    "assess_row",
    "build_handoff_report",
    "build_decision_packet",
    "build_decision_record",
    "build_intake_plan",
    "build_intake_receipt",
    "run_package_builder",
    "build_snapshot",
    "exit_code_for",
    "decision_packet",
    "handoff",
    "readiness_runner",
)
MONITORING_PUBLIC_WITNESS: tuple[str, ...] = (
    "PHASE3_3_BLOCKER_CODE",
    "READINESS_SCHEMA_VERSION",
    "HealthReport",
    "BatchQuantification",
    "EvidenceReadinessReport",
    "CheckStatus",
    "build_readiness_report",
    "summarize_batch",
    "load_qualification_report",
    "render_monitoring_report",
    "collector_health",
    "evidence_readiness",
)


@pytest.mark.parametrize("package_name", PACKAGES)
def test_all_matches_lazy_export_table(package_name: str) -> None:
    """``__all__`` 与惰性表必须**同源**（否则公开 API 会静默缺名 / 多名）。"""
    package = importlib.import_module(package_name)
    assert set(package.__all__) == set(package._LAZY_EXPORTS)
    assert len(package.__all__) == len(set(package.__all__))


@pytest.mark.parametrize("package_name", PACKAGES)
def test_every_public_name_resolves_to_its_defining_submodule(package_name: str) -> None:
    """每个公开名都必须 **is** 其定义子模块上的对象（证明没有第二份事实源）。"""
    package = importlib.import_module(package_name)
    for name in package.__all__:
        submodule_name, attribute = package._LAZY_EXPORTS[name]
        submodule = importlib.import_module(f"{package_name}.{submodule_name}")
        assert getattr(package, name) is getattr(submodule, attribute), name


@pytest.mark.parametrize("package_name", PACKAGES)
def test_alias_table_points_at_real_aliases(package_name: str) -> None:
    """别名必须真的换了名字，且与惰性总表一致（例如 ``*_exit_code_for``）。"""
    package = importlib.import_module(package_name)
    for public, target in package._LAZY_EXPORT_ALIASES.items():
        submodule_name, attribute = target
        assert public != attribute
        assert package._LAZY_EXPORTS[public] == (submodule_name, attribute)


@pytest.mark.parametrize("package_name", PACKAGES)
def test_lazy_export_caches_into_module_dict(package_name: str) -> None:
    """惰性导出只在首次访问时解析，随后缓存进模块字典（与 eager 版的访问代价一致）。"""
    package = importlib.import_module(package_name)
    first = package.__all__[0]
    value = getattr(package, first)
    assert package.__dict__[first] is value


@pytest.mark.parametrize(
    ("package", "witness"),
    ((src.evidence, EVIDENCE_PUBLIC_WITNESS), (src.monitoring, MONITORING_PUBLIC_WITNESS)),
)
def test_existing_public_exports_remain_available(
    package: object, witness: tuple[str, ...]
) -> None:
    missing = [name for name in witness if not hasattr(package, name)]
    assert missing == []


def test_submodule_names_stay_accessible() -> None:
    """``from src.<pkg> import <子模块>`` / ``import src.<pkg>`` 后取属性都要保持可用。"""
    assert src.evidence.decision_packet is importlib.import_module("src.evidence.decision_packet")
    assert src.evidence.readiness_runner is importlib.import_module(
        "src.evidence.readiness_runner"
    )
    assert src.monitoring.evidence_readiness is importlib.import_module(
        "src.monitoring.evidence_readiness"
    )
    assert src.monitoring.phase33_qualification is importlib.import_module(
        "src.monitoring.phase33_qualification"
    )


def test_unknown_names_raise_attribute_error() -> None:
    """未登记的名字必须抛 ``AttributeError``：绝不静默返回 None、绝不吞掉 ImportError。"""
    for package in (src.evidence, src.monitoring):
        with pytest.raises(AttributeError):
            _ = package.not_a_real_export_9f2  # type: ignore[attr-defined]


def test_dir_exposes_public_api() -> None:
    for package in (src.evidence, src.monitoring):
        assert set(package.__all__) <= set(dir(package))


def test_repeated_import_returns_same_module_object() -> None:
    """重复导入必须是幂等的（模块缓存不失效、不重新执行包初始化）。"""
    assert importlib.import_module("src.evidence") is src.evidence
    assert importlib.import_module("src.monitoring") is src.monitoring


def test_package_inits_have_no_eager_submodule_imports() -> None:
    """源码级守卫：包 ``__init__`` 模块级**只允许** ``__future__`` / ``importlib`` / ``typing``。

    这是本次循环导入的**根因门禁**：一旦有人把
    ``from src.evidence.xxx import ...``（或 ``from src.monitoring.xxx import ...``）的 eager
    导入写回来，包初始化阶段就会再次加载整个依赖图，循环导入随时回归。
    """
    for path in PACKAGE_INITS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert node.module in ALLOWED_INIT_IMPORTS, (path.name, node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name in ALLOWED_INIT_IMPORTS, (path.name, alias.name)
