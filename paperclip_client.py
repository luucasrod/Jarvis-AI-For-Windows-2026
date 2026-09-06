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
    if _CONFIG_TOKEN:
        return {"Authorization": f"Bearer {_CONFIG_TOKEN}"}
    return {}


def _get(path: str):
    """GET relativo à API. Devolve (dados, erro) — só um dos dois é not-None."""
    try:
        r = requests.get(f"{BASE_URL}{path}", headers=_headers(), timeout=_TIMEOUT)
    except requests.exceptions.ConnectionError:
        return None, "offline"
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, f"erro de rede ({e})"

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


def is_available() -> bool:
    """Checagem rápida (usada pra decidir se vale a pena tentar o relatório)."""
    _, err = _get("/api/health")
    return err is None


# ─── CAMADA DE ESCRITA (comandos) ─────────────────────────────────────────────
# Superfície deliberadamente pequena: só ações seguras e reversíveis. Nada de
# apagar empresa, mudar orçamento, contratar ou terminar agente permanentemente
# — isso fica pra uma fase futura, com guardrails próprios. Toda chamada aqui
# é feita só depois de confirmação falada do usuário (ver main.py).

def _post(path: str, body: dict | None = None):
    try:
        r = requests.post(
            f"{BASE_URL}{path}",
            json=body or {},
            headers={**_headers(), "Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        return None, "offline"
    except requests.exceptions.Timeout:
        return None, "timeout"
    except Exception as e:
        return None, f"erro de rede ({e})"

    if r.status_code in (401, 403):
        return None, "autenticação recusada"
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", r.text[:200])
        except Exception:
            detail = r.text[:200]
        return None, f"Paperclip recusou ({r.status_code}: {detail})"

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


def create_task(company_id: str, title: str, description: str = "", assignee_agent_id: str | None = None):
    body = {"title": title, "description": description, "priority": "medium"}
    if assignee_agent_id:
        body["assigneeAgentId"] = assignee_agent_id
    return _post(f"/api/companies/{company_id}/issues", body)
