"""日志系统与 YAML 资产测试（PyYAML 为项目正式依赖的落地点，02 §4）。

PyYAML 负责解析 ``config/logging.yaml`` 与仓库内的 YAML 资产（例如 CI workflow）。
本文件同时验证"所有 YAML 资产都能被解析"，避免配置语法错误在运行时才暴露。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from config.logging import configure_logging, get_logger, load_logging_config

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {
    ".venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".codex-tmp",
    ".postgres-local",
    "data",
    "logs",
}


def test_load_logging_config_returns_mapping() -> None:
    config = load_logging_config()

    assert config["version"] == 1
    assert "console" in config["handlers"]
    assert config["loggers"]["gold_ai"]["level"] == "INFO"


def test_load_logging_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_logging_config(tmp_path / "nope.yaml")


def test_load_logging_config_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(ValueError, match="YAML 映射"):
        load_logging_config(path)


def test_get_logger_uses_namespace() -> None:
    assert get_logger("collectors.runner").name == "gold_ai.collectors.runner"
    assert get_logger("gold_ai").name == "gold_ai"
    assert get_logger("gold_ai.x").name == "gold_ai.x"


def test_configure_logging_applies_level_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "logging.yaml"
    path.write_text(
        "version: 1\n"
        "disable_existing_loggers: false\n"
        "handlers:\n"
        "  console:\n"
        "    class: logging.StreamHandler\n"
        "loggers:\n"
        "  gold_ai:\n"
        "    level: WARNING\n"
        "    handlers: [console]\n"
        "root:\n"
        "  level: WARNING\n"
        "  handlers: [console]\n",
        encoding="utf-8",
    )
    logger = logging.getLogger("gold_ai")
    original_level = logger.level
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)
    try:
        configure_logging(level="DEBUG", path=path, force=True)
        assert logger.level == logging.DEBUG
        configure_logging()  # 幂等：二次调用不抛错
    finally:
        # 还原日志状态：dictConfig 会把 gold_ai 的 propagate 置为 False 并挂上 handler，
        # 若不还原会污染其他测试（影响它们的 caplog 捕获）。这是测试隔离要求。
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        for handler in original_handlers:
            logger.addHandler(handler)


def test_all_repo_yaml_assets_are_parseable() -> None:
    yaml_files = [
        path
        for path in REPO_ROOT.rglob("*.y*ml")
        if not any(
            part in SKIP_DIRS or part.startswith((".pytest-temp-", ".pytest-cache-"))
            for part in path.parts
        )
    ]
    names = {path.name for path in yaml_files}
    assert "logging.yaml" in names
    assert "ci.yml" in names

    for path in yaml_files:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert parsed is not None, f"{path.name} 解析为空"


def test_ci_workflow_declares_lint_typecheck_and_tests() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )

    assert set(workflow["jobs"]) == {"quality", "postgres"}
    quality_steps = [step.get("run", "") for step in workflow["jobs"]["quality"]["steps"]]
    assert any("ruff" in step for step in quality_steps)
    assert any("mypy" in step for step in quality_steps)
    assert any("pytest" in step for step in quality_steps)

    postgres_steps = [step.get("run", "") for step in workflow["jobs"]["postgres"]["steps"]]
    assert any("check_pg_schema.py" in step for step in postgres_steps)
