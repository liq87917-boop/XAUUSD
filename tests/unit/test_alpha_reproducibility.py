import pandas as pd
import pytest

from src.alpha.reproducibility import (
    ReproducibilityError,
    combined_digest,
    frame_digest,
    require_identical,
)


def test_frame_digest_is_stable_and_content_sensitive() -> None:
    frame = pd.DataFrame(
        {"value": [1.0, 2.0]},
        index=pd.to_datetime(["2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"]),
    )
    first = frame_digest(frame, ("value",))
    assert frame_digest(frame.copy(), ("value",)) == first
    changed = frame.copy()
    changed.iloc[1, 0] = 2.1
    assert frame_digest(changed, ("value",)) != first


def test_combined_digest_is_ordered() -> None:
    assert combined_digest("a", "b") == combined_digest("a", "b")
    assert combined_digest("a", "b") != combined_digest("b", "a")


def test_require_identical_rejects_any_difference() -> None:
    require_identical({"metric": 1.0}, {"metric": 1.0}, label="test")
    with pytest.raises(ReproducibilityError, match="test"):
        require_identical({"metric": 1.0}, {"metric": 1.0000001}, label="test")
