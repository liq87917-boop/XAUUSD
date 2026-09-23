from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import ai_orchestrator as orch


@pytest.fixture
def queue_fs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    tasks = tmp_path / "tasks"
    results = tmp_path / "results"
    tasks.mkdir()
    results.mkdir()
    monkeypatch.setattr(orch, "TASK_DIR", tasks)
    monkeypatch.setattr(orch, "RESULT_DIR", results)
    return tasks, results


def write_task(
    tasks: Path,
    task_id: str,
    **extra: object,
) -> Path:
    payload: dict[str, object] = {
        "task_id": task_id,
        "title": task_id,
        **extra,
    }
    path = tasks / f"{task_id}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_result(
    results: Path,
    task_id: str,
    status: str,
) -> None:
    (results / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "status": status,
            }
        ),
        encoding="utf-8",
    )


def test_dependency_completed_allows_auto_start(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    write_task(tasks, "GOLD-017")
    write_result(results, "GOLD-017", "completed")
    task = {
        "task_id": "GOLD-018",
        "depends_on": ["GOLD-017"],
        "auto_start": True,
    }

    ready, reason = orch.evaluate_task_readiness(task)

    assert ready is True
    assert reason == "ready"


def test_blocked_dependency_stops_queue(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    write_task(tasks, "GOLD-017")
    write_result(results, "GOLD-017", "blocked")
    task = {
        "task_id": "GOLD-018",
        "depends_on": ["GOLD-017"],
    }

    ready, reason = orch.evaluate_task_readiness(task)

    assert ready is False
    assert "dependency blocked" in reason


def test_missing_dependency_fails_closed(
    queue_fs: tuple[Path, Path],
) -> None:
    task = {
        "task_id": "GOLD-018",
        "depends_on": ["GOLD-017"],
    }

    ready, reason = orch.evaluate_task_readiness(task)

    assert ready is False
    assert "dependency missing" in reason


@pytest.mark.parametrize("gate", ["L3", "l4", {"level": "L3"}])
def test_l3_l4_human_gate_stops_auto_start(
    queue_fs: tuple[Path, Path],
    gate: object,
) -> None:
    task = {
        "task_id": "GOLD-018",
        "human_gate": gate,
    }

    ready, reason = orch.evaluate_task_readiness(task)

    assert ready is False
    assert "human_gate=" in reason


def test_l2_gate_can_remain_in_rolling_queue(
    queue_fs: tuple[Path, Path],
) -> None:
    task = {
        "task_id": "GOLD-018",
        "human_gate": "L2",
    }

    ready, reason = orch.evaluate_task_readiness(task)

    assert ready is True
    assert reason == "ready"


def test_auto_start_false_waits_for_explicit_action(
    queue_fs: tuple[Path, Path],
) -> None:
    task = {
        "task_id": "GOLD-018",
        "auto_start": False,
    }

    ready, reason = orch.evaluate_task_readiness(task)

    assert ready is False
    assert "auto_start=false" in reason


def test_queue_does_not_skip_first_waiting_task(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    write_task(
        tasks,
        "GOLD-018",
        depends_on=["GOLD-099"],
    )
    write_task(
        tasks,
        "GOLD-019",
    )

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert "GOLD-018" in reason
    assert "dependency missing" in reason


def test_completed_tasks_are_skipped_and_next_ready_runs(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    write_task(tasks, "GOLD-017")
    write_result(results, "GOLD-017", "completed")
    expected = write_task(
        tasks,
        "GOLD-018",
        depends_on=["GOLD-017"],
    )

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file == expected
    assert reason == "ready"


def test_queue_snapshot_excludes_terminal_results(
    queue_fs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks, results = queue_fs
    monkeypatch.setattr(orch, "QUEUE_TARGET_SIZE", 3)
    write_task(tasks, "GOLD-017")
    write_result(results, "GOLD-017", "completed")
    write_task(tasks, "GOLD-018")
    write_task(tasks, "GOLD-019")
    write_result(results, "GOLD-019", "blocked")
    write_task(tasks, "GOLD-020")

    snapshot = orch.queue_snapshot()

    assert snapshot == {
        "pending": ["GOLD-018", "GOLD-020"],
        "count": 2,
        "target": 3,
    }


def test_idle_log_throttle_does_not_change_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "IDLE_LOG_SECONDS", 1800)

    assert orch.should_log_idle(None, current_time=100.0) is True
    assert orch.should_log_idle(100.0, current_time=1899.0) is False
    assert orch.should_log_idle(100.0, current_time=1900.0) is True


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="依赖自身"):
        orch.get_task_dependencies(
            {
                "task_id": "GOLD-018",
                "depends_on": ["GOLD-018"],
            }
        )
