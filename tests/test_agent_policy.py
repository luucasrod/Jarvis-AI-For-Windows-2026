"""Tests for orchestrator.agent_policy (issue #24)."""
from orchestrator.agent_policy import classify_task
from orchestrator.models import AgentClass, AgentName, ExecutionMode, Task


def test_architecture_task_prefers_claude():
    task = Task(title="Definir arquitetura do planner", objective="Desenhar a arquitetura do pipeline de planejamento")
    assignment = classify_task(task)
    assert assignment.agent_class == AgentClass.CLAUDE
    assert assignment.preferred_agent == AgentName.CLAUDE
    assert assignment.fallback_agent == AgentName.CODEX


def test_mechanical_task_prefers_codex():
    task = Task(title="Cliente GitHub", objective="Wrapper de cliente HTTP para criar issues via CRUD basico")
    assignment = classify_task(task)
    assert assignment.agent_class == AgentClass.CODEX
    assert assignment.preferred_agent == AgentName.CODEX
    assert assignment.fallback_agent == AgentName.CLAUDE


def test_ambiguous_task_is_flex():
    task = Task(title="Ajustar mensagem de erro", objective="Melhorar o texto de uma mensagem de erro exibida ao usuario")
    assignment = classify_task(task)
    assert assignment.agent_class == AgentClass.FLEX


def test_reviewer_is_always_opposite_of_preferred_claude():
    task = Task(title="Decisao de seguranca critica", objective="Definir a politica de seguranca do sistema")
    assignment = classify_task(task)
    assert assignment.preferred_agent == AgentName.CLAUDE
    assert assignment.reviewer_preference == AgentName.CODEX


def test_reviewer_is_always_opposite_of_preferred_codex():
    task = Task(title="Parser de log", objective="Escrever um parser/wrapper para extrair dados de log")
    assignment = classify_task(task)
    assert assignment.preferred_agent == AgentName.CODEX
    assert assignment.reviewer_preference == AgentName.CLAUDE


def test_hotspot_file_forces_solo_regardless_of_class():
    task = Task(title="Ajuste no main.py", objective="Adicionar um novo intent de voz em main.py")
    assignment = classify_task(task)
    assert assignment.execution_mode == ExecutionMode.SOLO


def test_hotspot_detected_via_probable_area():
    task = Task(title="Nova funcionalidade", objective="Alterar o dispatcher de comandos", probable_area="main.py")
    assignment = classify_task(task)
    assert assignment.execution_mode == ExecutionMode.SOLO


def test_non_hotspot_task_keeps_parallel_execution_mode():
    task = Task(title="Novo modulo isolado", objective="Criar um modulo novo em orchestrator/")
    assignment = classify_task(task)
    assert assignment.execution_mode == ExecutionMode.PARALLEL


def test_flex_task_prefers_less_loaded_agent():
    task = Task(title="Ajustar mensagem de erro", objective="Melhorar texto de mensagem de erro")
    assignment = classify_task(task, current_load={"Claude": 5, "Codex": 1})
    assert assignment.preferred_agent == AgentName.CODEX
    assert assignment.reviewer_preference == AgentName.CLAUDE


def test_flex_task_prefers_claude_when_codex_more_loaded():
    task = Task(title="Ajustar mensagem de erro", objective="Melhorar texto de mensagem de erro")
    assignment = classify_task(task, current_load={"Claude": 1, "Codex": 5})
    assert assignment.preferred_agent == AgentName.CLAUDE


def test_flex_task_with_equal_load_is_either():
    task = Task(title="Ajustar mensagem de erro", objective="Melhorar texto de mensagem de erro")
    assignment = classify_task(task, current_load={"Claude": 2, "Codex": 2})
    assert assignment.preferred_agent == AgentName.EITHER
    assert assignment.reviewer_preference == AgentName.EITHER
    assert assignment.fallback_agent == AgentName.NONE


def test_flex_task_with_no_load_info_defaults_to_either():
    task = Task(title="Ajustar mensagem de erro", objective="Melhorar texto de mensagem de erro")
    assignment = classify_task(task)
    assert assignment.preferred_agent == AgentName.EITHER


def test_solo_flex_combination_is_valid_per_section_67():
    # A task can be FLEX + SOLO + preferred Claude at the same time -
    # SOLO is orthogonal to agent_class (section 67).
    task = Task(title="Ajuste isolado no main.py", objective="Pequeno ajuste generico em main.py", probable_area="main.py")
    assignment = classify_task(task, current_load={"Claude": 3, "Codex": 1})
    assert assignment.agent_class == AgentClass.FLEX
    assert assignment.execution_mode == ExecutionMode.SOLO
    assert assignment.preferred_agent == AgentName.CODEX
