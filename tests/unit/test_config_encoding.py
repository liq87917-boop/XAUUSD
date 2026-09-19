"""配置文件编码约束（团队决定：所有配置文件必须 UTF-8 无 BOM）。

背景：zh-CN Windows 上 Python 的 ``configparser`` / ``logging.fileConfig`` 使用平台
默认编码（GBK）读取 ``.ini``。含非 ASCII 字符的 ``alembic.ini`` 曾导致
``UnicodeDecodeError``，使迁移完全无法执行。为彻底避免同类问题：

- ``.ini``：必须**纯 ASCII**（最保守、跨平台最稳）；
- ``.toml`` / ``.yaml`` / ``.json`` / ``.env.example``：必须 **UTF-8 无 BOM**；
- 任何文件都禁止 UTF-8 BOM 与 UTF-16。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

CONFIG_PATTERNS = ("*.ini", "*.toml", "*.cfg", "*.yaml", "*.yml", "*.json", "*.mako")
SKIP_DIRS = {
    ".venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".codex-tmp",
    ".postgres-local",
    "node_modules",
    "gold_ai.egg-info",
    "data",
    "logs",
}
SKIP_PREFIXES = (".pytest-temp-", ".pytest-cache-")


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS or part.startswith(SKIP_PREFIXES) for part in path.parts)


def test_generated_pytest_artifacts_are_not_configuration_inputs() -> None:
    for directory in (".pytest-temp-example", ".pytest-cache-example", "logs", "data"):
        assert _is_skipped(REPO_ROOT / directory / "alembic.ini")
    assert not _is_skipped(REPO_ROOT / "config" / "logging.yaml")


def _config_files() -> list[Path]:
    files: list[Path] = []
    for pattern in CONFIG_PATTERNS:
        files.extend(path for path in REPO_ROOT.rglob(pattern) if not _is_skipped(path))
    return sorted(files)


def test_config_files_are_discovered() -> None:
    names = {path.name for path in _config_files()}
    assert {"alembic.ini", "pyproject.toml"} <= names


@pytest.mark.parametrize("path", _config_files(), ids=lambda p: p.name)
def test_config_file_is_utf8_without_bom(path: Path) -> None:
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 含 UTF-8 BOM，必须使用无 BOM"
    assert not raw.startswith((b"\xff\xfe", b"\xfe\xff")), f"{path.name} 疑似 UTF-16"
    raw.decode("utf-8")


def test_ini_files_are_pure_ascii() -> None:
    """.ini 会被 configparser 以平台默认编码读取（zh-CN Windows = GBK），必须纯 ASCII。"""
    for path in sorted(REPO_ROOT.rglob("*.ini")):
        if _is_skipped(path):
            continue
        try:
            path.read_bytes().decode("ascii")
        except UnicodeDecodeError as exc:  # pragma: no cover - 仅在有人引入非 ASCII 时触发
            pytest.fail(f"{path.name} 含非 ASCII 字符（{exc}），会在 GBK 环境解析失败")


def test_env_example_is_utf8_without_bom() -> None:
    example = REPO_ROOT / ".env.example"
    assert example.exists(), ".env.example 必须存在（Phase 0/1 交付物）"
    raw = example.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("utf-8")


def test_env_example_keeps_safety_flags_false() -> None:
    """实盘安全门禁不得在示例配置里被打开（.clinerules 第二条）。"""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "LIVE_TRADING=false" in text
    assert "ALLOW_EXTERNAL_ORDER_SUBMISSION=false" in text
