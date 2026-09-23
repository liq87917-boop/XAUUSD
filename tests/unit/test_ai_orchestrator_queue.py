from __future__ import annotations

import json
import os
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


# ============================================================
# GOLD-019：依赖图校验与 fail-closed 停线语义
# ============================================================


def test_defaults_keep_20s_polling_queue_target_and_idle_throttle() -> None:
    poll_expected = int(os.environ.get("AI_POLL_SECONDS", "20"))
    queue_expected = max(
        1,
        int(os.environ.get("AI_QUEUE_TARGET_SIZE", "3")),
    )
    idle_expected = max(
        orch.POLL_SECONDS,
        int(os.environ.get("AI_IDLE_LOG_SECONDS", "1800")),
    )

    assert poll_expected == orch.POLL_SECONDS
    assert queue_expected == orch.QUEUE_TARGET_SIZE
    assert idle_expected == orch.IDLE_LOG_SECONDS

    if "AI_POLL_SECONDS" not in os.environ:
        assert orch.POLL_SECONDS == 20
    if "AI_QUEUE_TARGET_SIZE" not in os.environ:
        assert orch.QUEUE_TARGET_SIZE == 3
    if "AI_IDLE_LOG_SECONDS" not in os.environ:
        assert orch.IDLE_LOG_SECONDS == 1800


def test_gate_vocabulary_and_terminal_statuses_unchanged() -> None:
    supported = set(orch.SUPPORTED_HUMAN_GATES)
    blocking = set(orch.BLOCKING_HUMAN_GATES)
    terminal = set(orch.TERMINAL_RESULT_STATUSES)

    assert supported == {"L1", "L2", "L3", "L4"}
    assert blocking == {"L3", "L4"}
    assert terminal == {"completed", "blocked"}
    assert blocking <= supported


def test_malformed_first_task_fails_closed_without_skipping(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    (tasks / "GOLD-018.json").write_text("{ not json", encoding="utf-8")
    write_task(tasks, "GOLD-019")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert "GOLD-018" in reason
    assert "invalid" in reason


def test_malformed_terminal_task_is_skipped_without_rewriting_result(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    (tasks / "GOLD-018.json").write_text("{ not json", encoding="utf-8")
    write_result(results, "GOLD-018", "completed")
    expected = write_task(tasks, "GOLD-019")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file == expected
    assert reason == "ready"

    history = json.loads(
        (results / "GOLD-018.json").read_text(encoding="utf-8")
    )
    assert history["status"] == "completed"


def test_task_id_filename_mismatch_fails_closed(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    (tasks / "GOLD-018.json").write_text(
        json.dumps({"task_id": "GOLD-999", "title": "mismatch"}),
        encoding="utf-8",
    )
    write_task(tasks, "GOLD-019")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert "GOLD-018" in reason
    assert "invalid" in reason


def test_self_dependency_stops_queue_without_skipping(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-018", depends_on=["GOLD-018"])
    write_task(tasks, "GOLD-019")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert reason.startswith("GOLD-018")
    assert "依赖自身" in reason


@pytest.mark.parametrize(
    "gate",
    ["L5", "L9", "auto", "3", 3, {"level": "L5"}, {"gate": "L1"}],
)
def test_unknown_human_gate_fails_closed(
    queue_fs: tuple[Path, Path],
    gate: object,
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-018", human_gate=gate)
    write_task(tasks, "GOLD-019")

    ready, reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-018", "human_gate": gate}
    )
    task_file, stop_reason = orch.find_next_task_with_reason()

    assert ready is False
    assert "metadata invalid" in reason
    assert "human_gate" in reason
    assert task_file is None
    assert stop_reason.startswith("GOLD-018")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("auto_start", "true"),
        ("auto_start", 1),
        ("auto_start", None),
        ("auto_start", []),
        ("requires_human_approval", "true"),
        ("requires_human_approval", 0),
    ],
)
def test_non_bool_task_flags_fail_closed(
    queue_fs: tuple[Path, Path],
    key: str,
    value: object,
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-018", **{key: value})
    write_task(tasks, "GOLD-019")

    ready, reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-018", key: value}
    )
    task_file, stop_reason = orch.find_next_task_with_reason()

    assert ready is False
    assert "metadata invalid" in reason
    assert key in reason
    assert task_file is None
    assert stop_reason.startswith("GOLD-018")


def test_bool_task_flags_still_work(queue_fs: tuple[Path, Path]) -> None:
    del queue_fs

    ready, reason = orch.evaluate_task_readiness(
        {
            "task_id": "GOLD-018",
            "auto_start": True,
            "requires_human_approval": False,
            "human_gate": "L1",
        }
    )

    assert ready is True
    assert reason == "ready"


def test_multi_node_dependency_cycle_fails_closed(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-018", depends_on=["GOLD-019"])
    write_task(tasks, "GOLD-019", depends_on=["GOLD-020"])
    write_task(tasks, "GOLD-020", depends_on=["GOLD-018"])

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert reason.startswith("GOLD-018")
    assert "dependency cycle" in reason

    expected_cycle = (
        "dependency cycle: "
        "GOLD-018 -> GOLD-019 -> GOLD-020 -> GOLD-018"
    )
    assert expected_cycle in reason
    assert orch.validate_dependency_graph() == [expected_cycle]


def test_indirect_dependency_cycle_fails_closed(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-018", depends_on=["GOLD-019"])
    write_task(tasks, "GOLD-019", depends_on=["GOLD-018"])

    ready, reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-018", "depends_on": ["GOLD-019"]}
    )

    assert ready is False
    assert "dependency cycle: GOLD-018 -> GOLD-019 -> GOLD-018" in reason


def test_transitive_missing_dependency_fails_closed(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-018", depends_on=["GOLD-019"])
    write_task(tasks, "GOLD-019", depends_on=["GOLD-099"])
    write_task(tasks, "GOLD-020")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert reason.startswith("GOLD-018")
    assert "dependency missing: GOLD-099" in reason
    assert "dependency missing: GOLD-099" in " ".join(
        orch.validate_dependency_graph()
    )


def test_blocked_dependency_blocks_all_dependents_without_rewriting_result(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    write_task(tasks, "GOLD-017")
    write_task(tasks, "GOLD-018", depends_on=["GOLD-017"])
    write_task(tasks, "GOLD-019", depends_on=["GOLD-018"])
    write_result(results, "GOLD-017", "blocked")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert reason.startswith("GOLD-018")
    assert "dependency blocked: GOLD-017" in reason

    # 间接依赖也被阻断，绝不越过 blocked 依赖
    ready, dependent_reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-019", "depends_on": ["GOLD-018"]}
    )

    assert ready is False
    assert "dependency blocked: GOLD-017" in dependent_reason

    history = json.loads(
        (results / "GOLD-017.json").read_text(encoding="utf-8")
    )
    assert history["status"] == "blocked"


def test_unknown_dependency_result_status_fails_closed(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    write_task(tasks, "GOLD-017")
    write_task(tasks, "GOLD-018", depends_on=["GOLD-017"])
    write_result(results, "GOLD-017", "frobnicated")

    ready, reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-018", "depends_on": ["GOLD-017"]}
    )

    assert ready is False
    assert "dependency status unknown" in reason
    assert "GOLD-017=frobnicated" in reason


def test_unknown_own_result_status_fails_closed(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    write_task(tasks, "GOLD-018")
    write_result(results, "GOLD-018", "frobnicated")
    write_task(tasks, "GOLD-019")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert reason.startswith("GOLD-018")
    assert "result status unknown" in reason


def test_legal_three_task_chain_runs_in_order(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, results = queue_fs
    first = write_task(tasks, "GOLD-101")
    second = write_task(tasks, "GOLD-102", depends_on=["GOLD-101"])
    third = write_task(tasks, "GOLD-103", depends_on=["GOLD-102"])

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file == first
    assert reason == "ready"

    # 后续任务绝不抢先执行
    ready, waiting_reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-102", "depends_on": ["GOLD-101"]}
    )

    assert ready is False
    assert "dependency not completed: GOLD-101=pending" in waiting_reason

    write_result(results, "GOLD-101", "completed")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file == second
    assert reason == "ready"

    write_result(results, "GOLD-102", "completed")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file == third
    assert reason == "ready"

    write_result(results, "GOLD-103", "completed")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert reason == "queue empty"


def test_queue_diagnostic_reports_pending_count_and_first_stop(
    queue_fs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks, results = queue_fs
    monkeypatch.setattr(orch, "QUEUE_TARGET_SIZE", 3)
    write_task(tasks, "GOLD-017")
    write_result(results, "GOLD-017", "completed")
    write_task(tasks, "GOLD-018", depends_on=["GOLD-099"])
    write_task(tasks, "GOLD-019")

    task_file, reason = orch.find_next_task_with_reason()
    diagnostic = orch.queue_diagnostic(reason)

    assert task_file is None
    assert "pending=2/3" in diagnostic
    assert "[GOLD-018, GOLD-019]" in diagnostic
    assert "first_stop=GOLD-018" in diagnostic
    assert f"reason={reason}" in diagnostic
    assert "dependency missing: GOLD-099" in diagnostic


def test_queue_diagnostic_when_queue_is_empty(
    queue_fs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "QUEUE_TARGET_SIZE", 3)
    del queue_fs

    task_file, reason = orch.find_next_task_with_reason()
    diagnostic = orch.queue_diagnostic(reason)

    assert task_file is None
    assert reason == "queue empty"
    assert "pending=0/3" in diagnostic
    assert "first_stop=-" in diagnostic


def test_queue_snapshot_counts_malformed_pending_task(
    queue_fs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks, results = queue_fs
    monkeypatch.setattr(orch, "QUEUE_TARGET_SIZE", 3)
    (tasks / "GOLD-018.json").write_text("{ not json", encoding="utf-8")
    write_task(tasks, "GOLD-019")
    write_result(results, "GOLD-019", "completed")

    snapshot = orch.queue_snapshot()

    assert snapshot == {
        "pending": ["GOLD-018"],
        "count": 1,
        "target": 3,
    }


@pytest.mark.parametrize(
    "value",
    [123, ["GOLD-017", 5], "", "   ", [""], {"GOLD-017": True}],
)
def test_invalid_depends_on_fails_closed(
    queue_fs: tuple[Path, Path],
    value: object,
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-018", depends_on=value)
    write_task(tasks, "GOLD-019")

    with pytest.raises(orch.TaskMetadataError):
        orch.get_task_dependencies(
            {"task_id": "GOLD-018", "depends_on": value}
        )

    ready, reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-018", "depends_on": value}
    )
    task_file, stop_reason = orch.find_next_task_with_reason()

    assert ready is False
    assert "metadata invalid" in reason
    assert "depends_on" in reason
    assert task_file is None
    assert stop_reason.startswith("GOLD-018")


def test_depends_on_accepts_single_string_and_absent_value(
    queue_fs: tuple[Path, Path],
) -> None:
    del queue_fs

    assert orch.get_task_dependencies({"task_id": "GOLD-018"}) == []
    assert (
        orch.get_task_dependencies(
            {"task_id": "GOLD-018", "depends_on": None}
        )
        == []
    )
    assert orch.get_task_dependencies(
        {"task_id": "GOLD-018", "depends_on": "GOLD-017"}
    ) == ["GOLD-017"]
    assert orch.get_task_dependencies(
        {
            "task_id": "GOLD-018",
            "depends_on": ["GOLD-017", "GOLD-017", "GOLD-016"],
        }
    ) == ["GOLD-017", "GOLD-016"]


def test_dependency_metadata_error_blocks_dependents(
    queue_fs: tuple[Path, Path],
) -> None:
    tasks, _ = queue_fs
    write_task(tasks, "GOLD-017", human_gate="L9")
    write_task(tasks, "GOLD-018", depends_on=["GOLD-017"])
    write_task(tasks, "GOLD-019")

    task_file, reason = orch.find_next_task_with_reason()

    assert task_file is None
    assert reason.startswith("GOLD-017")
    assert "human_gate 未知档位: L9" in reason

    # 依赖它的任务同样被依赖图闭包校验挡住，绝不越过坏依赖执行
    ready, dependent_reason = orch.evaluate_task_readiness(
        {"task_id": "GOLD-018", "depends_on": ["GOLD-017"]}
    )

    assert ready is False
    assert "human_gate 未知档位: L9" in dependent_reason
