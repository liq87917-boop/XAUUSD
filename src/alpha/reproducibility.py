"""Alpha Lab 研究输入的稳定内容指纹。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

import pandas as pd


def frame_digest(frame: pd.DataFrame, columns: Sequence[str]) -> str:
    """对排序后的指定列计算稳定 SHA-256；索引作为输入身份的一部分。"""
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"指纹列缺失：{sorted(missing)}")
    selected = frame.loc[:, list(columns)].sort_index().reset_index()
    payload = selected.to_csv(
        index=False,
        date_format="%Y-%m-%dT%H:%M:%S.%f%z",
        float_format="%.12g",
        lineterminator="\n",
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def combined_digest(*digests: str) -> str:
    """按调用顺序合并多个已计算指纹。"""
    return hashlib.sha256("\n".join(digests).encode("ascii")).hexdigest()


class ReproducibilityError(RuntimeError):
    """同一冻结输入重复运行得到不同结果。"""


def require_identical(first: Any, second: Any, *, label: str) -> None:
    """结果必须逐字段相等；不允许用容差掩盖非确定性。"""
    if first != second:
        raise ReproducibilityError(f"{label} 重复运行结果不一致")
