"""Tests for orchestrator.decisions (issue #33)."""
from orchestrator.config import OrchestratorConfig
from orchestrator.decisions import (
    create_pending_decision,
    format_decision_message,
    notify_needs_lucas,
    resolve_pending_decision,
)
from orchestrator.models import Task
from orchestrator.persistence import Store

_CONFIGURED = OrchestratorConfig(
    telegram_bot_token="fake-token",
    telegram_control_chat_id="111",
    telegram_report_chat_id="222",
)


def test_format_decision_message_follows_exact_template():
    task = Task(title="Mudar plano", objective="Mudar o plano de billing", project_id="cashy")
    message = format_decision_message(
        task,
        problem="Precisa mudar de plano no Stripe",
        why="Envolve custo recorrente, decisao financeira",
        options=["Manter plano atual", "Mudar pra Pro", "Cancelar"],
        recommendation="Manter o atual por enquanto",
        impact="Sem impacto imediato se adiado",
    )
    assert message.startswith("PROJETO:\ncashy")
    assert "CONTEXTO:" in message
    assert "PROBLEMA:\nPrecisa mudar de plano no Stripe" in message
    assert "POR QUE PRECISA DE MIM:\nEnvolve custo recorrente, decisao financeira" in message
    assert "OPCOES:\nA) Manter plano atual\nB) Mudar pra Pro\nC) Cancelar" in message
    assert "RECOMENDACAO:\nManter o atual por enquanto" in message
    assert "IMPACTO:\nSem impacto imediato se adiado" in message


def test_format_decision_message_uses_context_when_present():
    task = Task(title="X", objective="obj generico", context="contexto detalhado real", project_id="argos")
    message = format_decision_message(task, "problema", "motivo", ["A"], "rec", "impacto")
    assert "CONTEXTO:\ncontexto detalhado real" in message


def test_format_decision_message_falls_back_to_objective_without_context():
    task = Task(title="X", objective="obj generico", project_id="argos")
    message = format_decision_message(task, "problema", "motivo", ["A"], "rec", "impacto")
    assert "CONTEXTO:\nobj generico" in message


def test_create_pending_decision_persists_even_without_telegram_credentials(tmp_path):
    store = Store(tmp_path / "state.db")
    task = Task(title="X", objective="obj", project_id="cashy")

    ok, error = create_pending_decision(task, "mensagem de decisao", store)
    # sem credenciais configuradas no ambiente de teste, o envio falha de
    # forma segura (ver #19) - mas a decisao TEM que ficar persistida de
    # qualquer forma, nunca perdida so porque o Telegram nao respondeu.
    assert ok is False
    assert error is not None
    pending = store.get_pending_decisions()
    assert len(pending) == 1
    assert pending[0]["task_id"] == task.id
    assert pending[0]["message"] == "mensagem de decisao"
    store.close()


def test_notify_needs_lucas_combines_format_and_persist(tmp_path):
    store = Store(tmp_path / "state.db")
    task = Task(title="Mudar billing", objective="Mudar plano de cobranca", project_id="cashy")

    notify_needs_lucas(
        task,
        problem="Decisao de billing",
        why="Envolve dinheiro",
        options=["A", "B"],
        recommendation="A",
        impact="baixo",
        store=store,
    )

    pending = store.get_pending_decisions()
    assert len(pending) == 1
    assert "PROBLEMA:\nDecisao de billing" in pending[0]["message"]
    store.close()


def test_resolve_pending_decision_returns_task_info_and_marks_resolved(tmp_path):
    store = Store(tmp_path / "state.db")
    task = Task(title="X", objective="obj", project_id="cashy")
    store.save_decision(correlation_id=task.correlation_id, task_id=task.id, message="msg")

    resolved = resolve_pending_decision(task.correlation_id, "opcao B", store)

    assert resolved is not None
    assert resolved["task_id"] == task.id
    assert store.get_pending_decisions() == []
    store.close()


def test_resolve_unknown_decision_returns_none(tmp_path):
    store = Store(tmp_path / "state.db")
    result = resolve_pending_decision("nao-existe", "resposta", store)
    assert result is None
    store.close()


def test_unrelated_pending_decisions_are_unaffected_by_resolving_one(tmp_path):
    store = Store(tmp_path / "state.db")
    task_a = Task(title="A", objective="obj a", project_id="cashy")
    task_b = Task(title="B", objective="obj b", project_id="argos")
    store.save_decision(correlation_id=task_a.correlation_id, task_id=task_a.id, message="msg a")
    store.save_decision(correlation_id=task_b.correlation_id, task_id=task_b.id, message="msg b")

    resolve_pending_decision(task_a.correlation_id, "resposta", store)

    remaining = store.get_pending_decisions()
    assert len(remaining) == 1
    assert remaining[0]["task_id"] == task_b.id
    store.close()
