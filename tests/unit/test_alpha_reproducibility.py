import pandas as pd

from src.alpha.reproducibility import combined_digest, frame_digest


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
