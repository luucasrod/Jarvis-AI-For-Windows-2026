"""Free-form conversation fallback for the Telegram control channel (#149).

The control channel (#19/#31) has a structured grammar (Objetivo:/REF:/
Prioriza) for real commands - decisions.py's `_route_control` still tries
that FIRST. This module only answers whatever text does NOT match that
grammar at all (the generic "nao consegui distinguir" case), using REAL
data - Paperclip's live snapshot (`paperclip_client.get_snapshot()`, the
SAME data source main.py's own `paperclip_report()` already uses) plus
the orchestrator's own Store - synthesized by an LLM. Never invents state:
if Paperclip is offline or a project has no data, that is said plainly in
the context handed to the model, never silently omitted.
"""
from __future__ import annotations

import paperclip_client
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.persistence import Store

_SYSTEM_PROMPT = (
    "Voce e o Jarvis, respondendo por escrito no canal de controle do Telegram "
    "para o Lucas. Voce recebeu abaixo um snapshot real (Paperclip: empresas, "
    "agentes, tarefas; e o orquestrador Jarvis: tarefas locais) em texto - "
    "NUNCA invente dado que nao esteja nele, e nunca leia formato tecnico "
    "(JSON, nomes de campos) em voz alta. Se a pergunta nao tiver relacao "
    "com projetos/agentes/tarefas, responda normalmente como assistente "
    "pessoal, de forma concisa e natural em portugues - nao force o "
    "snapshot em respostas que nao precisam dele. Sempre chame o usuario "
    "de 'senhor'."
)
_NO_ANSWER = "Nao consegui gerar uma resposta agora, senhor."
_UNAVAILABLE = "Nao consegui responder agora, senhor - tive um erro ao consultar o modelo."
_NO_KEY = "Ainda nao tenho uma chave da Groq configurada pra responder perguntas livres, senhor."


def _snapshot_text(snapshot: dict) -> str:
    """Same reduction main.py's own `_paperclip_snapshot_text` uses -
    compact enough to hand an LLM without sending raw JSON."""
    if not snapshot.get("available"):
        return f"Paperclip indisponivel agora ({snapshot.get('reason', 'motivo desconhecido')})."
    companies = snapshot.get("companies", [])
    if not companies:
        return "Paperclip esta online, mas ainda nao existe nenhuma empresa/projeto configurado."
    lines = []
    for company in companies:
        lines.append(f"Empresa: {company.get('name')}")
        for agent in company.get("agents", []):
            bits = [f"status={agent.get('status')}"]
            if agent.get("pause_reason"):
                bits.append(f"pausado_por={agent['pause_reason']}")
            if agent.get("error_reason"):
                bits.append(f"erro={agent['error_reason']}")
            lines.append(f"  Agente {agent.get('name')} ({agent.get('role', 'general')}): {', '.join(bits)}")
        by_status = company.get("issues_by_status") or {}
        if by_status:
            lines.append("  Tarefas por status: " + ", ".join(f"{k}={v}" for k, v in by_status.items()))
        for issue in company.get("open_issues", []):
            ident = issue.get("identifier") or issue.get("id", "")
            lines.append(f"  Aberta [{issue.get('status')}] {ident}: {issue.get('title', '')}")
    return "\n".join(lines)


def _store_text(store: Store) -> str:
    tasks = store.list_tasks()
    if not tasks:
        return "Orquestrador Jarvis: nenhuma tarefa local registrada ainda."
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.state.value] = counts.get(task.state.value, 0) + 1
    return "Orquestrador Jarvis, tarefas locais por estado: " + ", ".join(
        f"{count} {state}" for state, count in counts.items()
    )


def answer_free_text(
    text: str, *, store: Store | None = None,
    config: OrchestratorConfig | None = None, chat_fn=None,
) -> str:
    """Answers free-form text grounded in the real Paperclip snapshot and
    the orchestrator's own Store, synthesized by an LLM. Never raises;
    degrades to an honest "could not answer" message on any failure
    (Paperclip unreachable is fine - that fact itself becomes context for
    the model; a missing/failing LLM is what falls back to _UNAVAILABLE/
    _NO_KEY).

    `chat_fn(text, context) -> str | None` is the injectable seam for
    tests (this package's own test/CI env does not have the `groq`
    package installed, matching telegram_bot.py's own pattern for voice
    transcription) - production omits it and uses a real Groq client."""
    config = config or load_config()
    snapshot = paperclip_client.get_snapshot()
    context = _snapshot_text(snapshot)
    if store is not None:
        context += "\n\n" + _store_text(store)

    if chat_fn is not None:
        try:
            return chat_fn(text, context) or _NO_ANSWER
        except Exception:
            return _UNAVAILABLE

    if not config.groq_api_key:
        return _NO_KEY
    try:
        from groq import Groq

        client = Groq(api_key=config.groq_api_key)
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Snapshot atual:\n{context}\n\nPergunta: {text}"},
            ],
            max_tokens=400,
            temperature=0.4,
        )
        answer = (response.choices[0].message.content or "").strip()
        return answer or _NO_ANSWER
    except Exception:
        return _UNAVAILABLE
