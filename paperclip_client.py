"""
Camada de integração somente-leitura com o Paperclip (orquestrador de agentes).

Paperclip roda local em http://127.0.0.1:3100 no modo "local_trusted" (loopback
only), então chamadas feitas a partir desta mesma máquina não precisam de
token — é a mesma trava de rede que já protege o painel web. Se isso mudar
(deploy remoto, modo autenticado), configure PAPERCLIP_API_TOKEN no config.py
e ele será enviado como Bearer automaticamente.

Este módulo nunca derruba o Jarvis: qualquer falha (Paperclip fechado, porta
errada, resposta inválida) vira um retorno estruturado com "available": False
e uma "reason" legível, nunca uma exceção não tratada.
"""
from __future__ import annotations

import os
from datetime import datetime
from urllib.parse import quote, urlencode

import requests

try:
    from config import paperclip_base_url as _CONFIG_BASE_URL
except ImportError:
    _CONFIG_BASE_URL = ""
try:
    from config import paperclip_api_token as _CONFIG_TOKEN
except ImportError:
    _CONFIG_TOKEN = ""

BASE_URL = (_CONFIG_BASE_URL or "http://127.0.0.1:3100").rstrip("/")
_TIMEOUT = 6.0


def _headers() -> dict:
    token = os.environ.get("PAPERCLIP_API_TOKEN") or _CONFIG_TOKEN
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _get(path: str, *, base_url: str | None = None, timeout: float | None = None):
    """GET relativo à API. Devolve (dados, erro) — só um dos dois é not-None."""
    try:
        r = requests.get(f"{(base_url or BASE_URL).rstrip('/')}{path}", headers=_headers(),
                         timeout=timeout if timeout is not None else _TIMEOUT)
    except requests.exceptions.ConnectionError:
        return None, "offline"
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception:
        return None, "erro de rede"

    if r.status_code == 401 or r.status_code == 403:
        return None, "autenticação recusada"
    if r.status_code >= 500:
        return None, f"erro no servidor do Paperclip ({r.status_code})"
    if r.status_code >= 400:
        return None, f"resposta inválida ({r.status_code})"

    try:
        return r.json(), None
    except ValueError:
        return None, "resposta inválida (não é JSON)"


def get_snapshot() -> dict:
    """
    Consolida o estado atual do Paperclip numa estrutura pronta pra virar
    resumo em linguagem natural. Nunca levanta exceção.

    Retorno sempre tem "available": bool.
    Se False, tem "reason": str (explicação curta, ex: "offline").
    Se True, tem "companies": list[dict], cada uma com:
        name, agents: [{name, role, status, pauseReason, errorReason,
                         budgetMonthlyCents, spentMonthlyCents}],
        issues_by_status: {status: count}, open_issues: [{id, title, status}],
        cost_usd_month: float
    """
    companies, err = _get("/api/companies")
    if err:
        return {"available": False, "reason": err}
    if not isinstance(companies, list):
        return {"available": False, "reason": "resposta inesperada (sem lista de empresas)"}
    if not companies:
        return {"available": True, "companies": []}

    result = []
    for c in companies:
        cid = c.get("id")
        name = c.get("name", "?")

        agents, agents_err = _get(f"/api/companies/{cid}/agents")
        agents = agents if isinstance(agents, list) else []

        issues, issues_err = _get(f"/api/companies/{cid}/issues")
        issues = issues if isinstance(issues, list) else []

        issues_by_status: dict[str, int] = {}
        open_issues = []
        for it in issues:
            status = it.get("status", "unknown")
            issues_by_status[status] = issues_by_status.get(status, 0) + 1
            if status not in ("done", "closed", "cancelled"):
                open_issues.append({
                    "id": it.get("id"),
                    "identifier": it.get("identifier"),
                    "title": it.get("title"),
                    "status": status,
                })

        result.append({
            "name": name,
            "budget_monthly_cents": c.get("budgetMonthlyCents", 0),
            "spent_monthly_cents": c.get("spentMonthlyCents", 0),
            "agents": [
                {
                    "name": a.get("name"),
                    "role": a.get("role"),
                    "status": a.get("status"),
                    "pause_reason": a.get("pauseReason"),
                    "error_reason": a.get("errorReason"),
                    "budget_monthly_cents": a.get("budgetMonthlyCents", 0),
                    "spent_monthly_cents": a.get("spentMonthlyCents", 0),
                }
                for a in agents
            ],
            "agents_fetch_error": agents_err,
            "issues_by_status": issues_by_status,
            "open_issues": open_issues[:15],
            "issues_fetch_error": issues_err,
        })

    return {"available": True, "companies": result}


def is_available(*, base_url: str | None = None, timeout: float | None = None) -> bool:
    """Checagem rápida (usada pra decidir se vale a pena tentar o relatório)."""
    data, err = _get("/api/health", base_url=base_url, timeout=timeout)
    return err is None and isinstance(data, dict) and data.get('status') == 'ok'


def get_runtime_info(*, base_url: str | None = None, timeout: float | None = None):
    """Read the process identity actually exposed by /api/health.

    Version alone cannot identify a restart. Older servers without serverInfo
    return a structured error; an outage is not proof of a restart either.
    Return only the identity, never unrelated deployment/authentication metadata.
    """
    data, error = _get('/api/health', base_url=base_url, timeout=timeout)
    if error:
        return None, error
    if not isinstance(data, dict) or data.get('status') != 'ok':
        return None, 'invalid_runtime_info'
    info = data.get('serverInfo')
    started = info.get('processStartedAt') if isinstance(info, dict) else None
    if not isinstance(started, str):
        return None, 'runtime_identity_unavailable'
    try:
        instant = datetime.fromisoformat(started.replace('Z', '+00:00'))
        if instant.utcoffset() is None:
            raise ValueError('timezone required')
    except ValueError:
        return None, 'invalid_runtime_identity'
    return {'process_started_at': instant}, None


# ─── CAMADA DE ESCRITA (comandos) ─────────────────────────────────────────────
# Superfície deliberadamente pequena: só ações seguras e reversíveis. Nada de
# apagar empresa, mudar orçamento, contratar ou terminar agente permanentemente
# — isso fica pra uma fase futura, com guardrails próprios. Toda chamada aqui
# é feita só depois de confirmação falada do usuário (ver main.py).

def _post(path: str, body: dict | None = None, *, base_url: str | None = None,
          timeout: float | None = None):
    try:
        r = requests.post(
            f"{(base_url or BASE_URL).rstrip('/')}{path}",
            json=body or {},
            headers={**_headers(), "Content-Type": "application/json"},
            timeout=timeout if timeout is not None else _TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        return None, "offline"
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception:
        return None, "erro de rede"

    if r.status_code in (401, 403):
        return None, "autenticação recusada"
    if r.status_code >= 400:
        return None, f"Paperclip recusou ({r.status_code})"

    try:
        return (r.json() if r.text else {}), None
    except ValueError:
        return {}, None


def find_agent(name_query: str) -> tuple[dict | None, str | None]:
    """Procura um agente pelo nome (exato, case-insensitive; se não achar
    exato, aceita substring) em todas as empresas.
    Devolve (agente_com__company_id, erro) — só um dos dois é not-None."""
    companies, err = _get("/api/companies")
    if err:
        return None, err
    if not isinstance(companies, list):
        return None, "resposta inesperada do Paperclip"

    q = name_query.strip().lower()
    partial_match = None
    for c in companies:
        agents, a_err = _get(f"/api/companies/{c['id']}/agents")
        if not isinstance(agents, list):
            continue
        for a in agents:
            a_name = (a.get("name") or "").strip().lower()
            if a_name == q:
                return {**a, "_company_id": c["id"]}, None
            if partial_match is None and (q in a_name or a_name in q):
                partial_match = {**a, "_company_id": c["id"]}

    if partial_match:
        return partial_match, None
    return None, f"não encontrei nenhum agente chamado '{name_query}'"


def pause_agent(agent_id: str):
    return _post(f"/api/agents/{agent_id}/pause")


def resume_agent(agent_id: str):
    return _post(f"/api/agents/{agent_id}/resume")


def create_task(company_id: str, title: str, description: str = "", assignee_agent_id: str | None = None,
                *, base_url: str | None = None, timeout: float | None = None):
    body = {"title": title, "description": description, "priority": "medium"}
    if assignee_agent_id:
        body["assigneeAgentId"] = assignee_agent_id
    return _post(f"/api/companies/{quote(company_id, safe='')}/issues", body,
                 base_url=base_url, timeout=timeout)


def list_company_tasks(company_id: str, *, query: str | None = None,
                       base_url: str | None = None, timeout: float | None = None):
    """Read company issues through the existing transport, with bounded paging.

    Returns (list, error). An incomplete/repeated page is an error rather than
    proof that an issue does not exist. Used for idempotency reconciliation.
    """
    collected, seen = [], set()
    for page in range(50):
        params = {"limit": 100, "offset": page * 100}
        if query:
            params["q"] = query
        data, error = _get(
            f"/api/companies/{quote(company_id, safe='')}/issues?{urlencode(params)}",
            base_url=base_url, timeout=timeout,
        )
        if error:
            return None, error
        if not isinstance(data, list):
            return None, "invalid_task_list"
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                return None, "invalid_task_list"
            if item["id"] in seen:
                return None, "incomplete_task_list"
            seen.add(item["id"])
            collected.append(item)
        if len(data) < 100:
            return collected, None
    return None, "incomplete_task_list"
