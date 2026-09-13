"""CLI 脚本的**真实进程**冒烟测试（subprocess；不依赖数据库、不联网）。

为什么必须用子进程：本项目的脚本既可以
`python scripts/xxx.py`（脚本模式）也可以 `python -m scripts.xxx`（模块模式）运行，
两者的 `sys.path` / `__package__` 不同。**单元测试里的 in-process 调用掩盖不了这类差异**
（曾真实踩坑：脚本模式下 `from scripts import ...` 直接 ModuleNotFoundError）。

另外这里也顺手守住控制台编码：Windows 中文环境的管道编码是 GBK，
任何 GBK 无法编码的字符（如 "⚠"）都会让脚本在打印时直接崩掉。
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.generate_mock_posts import POST_COLUMNS

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "generate_mock_posts.py"
SAMPLER = REPO_ROOT / "scripts" / "sample_annotation_set.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """以真实进程运行（不注入 PYTHONPATH，模拟用户手工执行的环境）。"""
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.mark.parametrize(
    "script",
    [
        GENERATOR,
        SAMPLER,
        REPO_ROOT / "scripts" / "compare_model_annotations.py",
    ],
)
def test_script_help_runs_in_script_mode(script: Path) -> None:
    result = _run(str(script), "--help")

    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


@pytest.mark.parametrize(
    "module",
    [
        "scripts.generate_mock_posts",
        "scripts.sample_annotation_set",
        "scripts.compare_model_annotations",
        "scripts._console",
    ],
)
def test_scripts_are_importable_as_modules(module: str) -> None:
    result = _run("-c", f"import importlib; importlib.import_module('{module}')")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""  # 导入不应有任何警告/异常


def test_generator_runs_and_writes_csv_in_script_mode(tmp_path: Path) -> None:
    """脚本模式生成：控制台编码不得炸（曾经因 "⚠" 在 GBK 下不可编码而崩溃）。"""
    out = tmp_path / "logs" / "posts.csv"

    result = _run(str(GENERATOR), "--out", str(out), "--count", "20")

    assert result.returncode == 0, result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    assert "mock-synthetic-v1" in result.stdout
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == list(POST_COLUMNS)
    assert len(rows) == 20


def test_sampler_auto_fills_missing_input_in_script_mode(tmp_path: Path) -> None:
    """用户的实际用法：输入不存在 → 自动生成 Mock → 抽样 200 条（这里用 40 条演示）。"""
    source = tmp_path / "logs" / "posts.csv"
    out = tmp_path / "sample.csv"

    result = _run(
        str(SAMPLER),
        "--input-csv",
        str(source),
        "--out",
        str(out),
        "--limit",
        "40",
        "--mock-fill",
        "40",
        "--media-quota",
        "0",
        "--require-full",
    )

    assert result.returncode == 0, result.stderr
    assert source.exists()
    assert "mock-synthetic-v1" in result.stderr  # 醒目警告写在 stderr
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 40


def test_sampler_full_run_on_generated_batch(tmp_path: Path) -> None:
    """端到端：生成 250 条 → 抽样 200 条（`--require-full` 必须退出码 0）。"""
    posts_csv = tmp_path / "posts.csv"
    out = tmp_path / "annotation_sample.csv"
    generated = _run(str(GENERATOR), "--out", str(posts_csv), "--count", "250")
    assert generated.returncode == 0, generated.stderr

    result = _run(
        str(SAMPLER),
        "--input-csv",
        str(posts_csv),
        "--out",
        str(out),
        "--limit",
        "200",
        "--require-full",
    )

    assert result.returncode == 0, result.stderr
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 200
    # 判定列一律留空（绝不代替人工标注）
    assert {row["stance"] for row in rows} == {""}
    assert {row["horizon"] for row in rows} == {""}
