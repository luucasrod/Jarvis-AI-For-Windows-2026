"""Core data models shared by the whole orchestrator (issue #12).

Deliberately plain stdlib dataclasses/enums - no heavy validation
framework (see PROMPT MESTRE V2 section 42: deterministic Python where
determinism suffices). Persistence (#13), state-transition logic (planner
#22, scheduler #25) all build on top of these types.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TaskState(str, Enum):
    INBOX = "INBOX"
    PLANNED = "PLANNED"
    NEXT_CYCLE = "NEXT_CYCLE"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    BLOCKED = "BLOCKED"
    NEEDS_LUCAS = "NEEDS_LUCAS"
    BUG_FOUND = "BUG_FOUND"
    DONE = "DONE"
    FAILED = "FAILED"


class AgentClass(str, Enum):
    """Who is meant to implement the task. SOLO is NOT a value here - it
    is an execution property (see ExecutionMode), independent of which
    agent does the work (PROMPT MESTRE V2 section 67)."""
    CLAUDE = "CLAUDE"
    CODEX = "CODEX"
    FLEX = "FLEX"


class ExecutionMode(str, Enum):
    PARALLEL = "PARALLEL"
    SOLO = "SOLO"


class AgentName(str, Enum):
    CLAUDE = "Claude"
    CODEX = "Codex"
    EITHER = "either"
    NONE = "none"


class TaskValidationError(ValueError):
    """Raised when a Task is constructed in an inconsistent state."""


def _reviewer_required(agent_class: AgentClass) -> bool:
    """CLAUDE/CODEX classes imply a preferred implementer, which in turn
    implies cross-review is expected (someone other than the implementer
    reviews). FLEX tasks may still require review; only truly reviewer-less
    tasks (reviewer_preference == NONE) are allowed to skip this, and only
    when explicitly marked as needing no review is out of this model's
    concern - callers set reviewer_preference=NONE deliberately."""
    return agent_class in (AgentClass.CLAUDE, AgentClass.CODEX, AgentClass.FLEX)


@dataclass
class Task:
    title: str
    objective: str
    context: str = ""
    project_id: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    probable_area: str | None = None
    dependencies: list[str] = field(default_factory=list)
    priority: str = "medium"
    risk: str = "low"
    agent_class: AgentClass = AgentClass.FLEX
    execution_mode: ExecutionMode = ExecutionMode.PARALLEL
    preferred_agent: AgentName = AgentName.EITHER
    fallback_agent: AgentName = AgentName.NONE
    reviewer_preference: AgentName = AgentName.EITHER
    test_plan: list[str] = field(default_factory=list)
    parent_task_id: str | None = None
    origin: str = "manual"
    state: TaskState = TaskState.INBOX
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.execution_mode == ExecutionMode.SOLO and _reviewer_required(self.agent_class):
            if self.reviewer_preference == AgentName.NONE:
                raise TaskValidationError(
                    "execution_mode=SOLO com agent_class que exige revisao "
                    "nao pode ter reviewer_preference=NONE"
                )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["agent_class"] = self.agent_class.value
        data["execution_mode"] = self.execution_mode.value
        data["preferred_agent"] = self.preferred_agent.value
        data["fallback_agent"] = self.fallback_agent.value
        data["reviewer_preference"] = self.reviewer_preference.value
        data["state"] = self.state.value
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        payload = dict(data)
        payload["agent_class"] = AgentClass(payload["agent_class"])
        payload["execution_mode"] = ExecutionMode(payload["execution_mode"])
        payload["preferred_agent"] = AgentName(payload["preferred_agent"])
        payload["fallback_agent"] = AgentName(payload["fallback_agent"])
        payload["reviewer_preference"] = AgentName(payload["reviewer_preference"])
        payload["state"] = TaskState(payload["state"])
        payload["created_at"] = datetime.fromisoformat(payload["created_at"])
        payload["updated_at"] = datetime.fromisoformat(payload["updated_at"])
        return cls(**payload)
