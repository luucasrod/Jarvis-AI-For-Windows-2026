"""Tests for orchestrator.models (issue #12)."""
import pytest

from orchestrator.models import (
    AgentClass,
    AgentName,
    ExecutionMode,
    Task,
    TaskState,
    TaskValidationError,
)


def test_task_state_enum_values():
    assert {s.value for s in TaskState} == {
        "INBOX", "PLANNED", "NEXT_CYCLE", "READY", "IN_PROGRESS", "IN_REVIEW",
        "BLOCKED", "NEEDS_LUCAS", "BUG_FOUND", "DONE", "FAILED",
    }


def test_agent_class_enum_values():
    assert {c.value for c in AgentClass} == {"CLAUDE", "CODEX", "FLEX"}


def test_execution_mode_enum_values():
    assert {m.value for m in ExecutionMode} == {"PARALLEL", "SOLO"}


def test_task_minimal_fields():
    task = Task(title="Fazer X", objective="Objetivo Y")
    assert task.title == "Fazer X"
    assert task.state == TaskState.INBOX
    assert task.agent_class == AgentClass.FLEX
    assert task.execution_mode == ExecutionMode.PARALLEL
    assert task.id
    assert task.correlation_id


def test_task_full_fields():
    task = Task(
        title="Implementar scheduler",
        objective="Ciclo 08:00/14:00/17:00",
        context="contexto detalhado",
        project_id="jarvis",
        acceptance_criteria=["dispara as 08:00", "nao dispara 2x"],
        probable_area="orchestrator/scheduler.py",
        dependencies=["task-123"],
        priority="high",
        risk="medium",
        agent_class=AgentClass.CODEX,
        execution_mode=ExecutionMode.PARALLEL,
        preferred_agent=AgentName.CODEX,
        fallback_agent=AgentName.CLAUDE,
        reviewer_preference=AgentName.CLAUDE,
        test_plan=["simular 08:00", "simular 14:00"],
        parent_task_id="epic-3",
        origin="planner",
        state=TaskState.PLANNED,
    )
    assert task.dependencies == ["task-123"]
    assert task.reviewer_preference == AgentName.CLAUDE


def test_round_trip_to_dict_and_back():
    original = Task(
        title="Round trip",
        objective="Testar serializacao",
        dependencies=["a", "b"],
        agent_class=AgentClass.CLAUDE,
        execution_mode=ExecutionMode.SOLO,
        preferred_agent=AgentName.CLAUDE,
        reviewer_preference=AgentName.CODEX,
        state=TaskState.READY,
    )
    data = original.to_dict()
    assert isinstance(data["agent_class"], str)
    assert isinstance(data["created_at"], str)

    restored = Task.from_dict(data)
    assert restored.title == original.title
    assert restored.dependencies == original.dependencies
    assert restored.agent_class == original.agent_class
    assert restored.execution_mode == original.execution_mode
    assert restored.reviewer_preference == original.reviewer_preference
    assert restored.state == original.state
    assert restored.created_at == original.created_at
    assert restored.id == original.id


def test_solo_without_reviewer_raises():
    with pytest.raises(TaskValidationError):
        Task(
            title="SOLO sem revisor",
            objective="Deveria falhar",
            agent_class=AgentClass.CLAUDE,
            execution_mode=ExecutionMode.SOLO,
            reviewer_preference=AgentName.NONE,
        )


def test_solo_with_reviewer_is_valid():
    task = Task(
        title="SOLO com revisor",
        objective="Deveria funcionar",
        agent_class=AgentClass.CLAUDE,
        execution_mode=ExecutionMode.SOLO,
        reviewer_preference=AgentName.CODEX,
    )
    assert task.execution_mode == ExecutionMode.SOLO


# --- Regression tests from Codex's review (Review Task #52, PR #51) --------
# Only AgentName.NONE was rejected for SOLO's reviewer_preference - None
# and "" slipped through at runtime (the type hint isn't enforced).

def test_solo_with_none_reviewer_raises():
    with pytest.raises(TaskValidationError):
        Task(
            title="Solo",
            objective="Task",
            agent_class=AgentClass.CLAUDE,
            execution_mode=ExecutionMode.SOLO,
            reviewer_preference=None,
        )


def test_solo_with_empty_string_reviewer_raises():
    with pytest.raises(TaskValidationError):
        Task(
            title="Solo",
            objective="Task",
            agent_class=AgentClass.CLAUDE,
            execution_mode=ExecutionMode.SOLO,
            reviewer_preference="",
        )
