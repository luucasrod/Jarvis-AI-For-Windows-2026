# CURRENT_STATE.md — Jarvis Engineering Harness

**Data da auditoria**: 2026-09-25
**Branches auditadas**: `main`, `integration/orchestration` (HEAD, `866c582`), `integration/wave-0` (congelada)
**Escopo**: `orchestrator/` em `Jarvis-AI-For-Windows-2026-integration-orchestration`, `main.py` em `Jarvis-AI-For-Windows-2026` (monólito de voz, processo `pythonw.exe` PID 10880 rodando ao vivo), Paperclip (app externa).

## 1. Dois codebases, ambos rodando agora

1. **Monólito de voz** (`Jarvis-AI-For-Windows-2026`, branch `main`) — Gemini/Groq chat, TTS (Piper/edge-tts/pyttsx3), hooks WiZ/eWeLink/Argos. `main.py` (~85KB) **não importa `orchestrator/`**.
2. **Orchestrator** (`Jarvis-AI-For-Windows-2026-integration-orchestration`, branch `integration/orchestration`) — dois processos `python -m orchestrator.runtime` rodando ao vivo, ponte Telegram↔orchestrator.

**Confirmado por git**: `integration/orchestration` é a base correta — 170 commits à frente de `main` (que não tem nenhum commit exclusivo), e `integration/wave-0` está oficialmente congelada (único commit exclusivo é um aviso de redirect). Nenhum trabalho é perdido usando `integration/orchestration` como base.

## 2. O que já existe e funciona (não recriar)

| Módulo | Maturidade | O que faz de fato |
|---|---|---|
| `scheduler.py` | Madura | `check_and_fire()` chamado externamente (não é loop próprio), dedup por dia via SQLite `run_sync_once`, `reconsider()` reavalia tarefa NEXT_CYCLE se dependência resolve no mesmo dia |
| `planner.py` | Madura | Pipeline real de 5 estágios: screen NEEDS_LUCAS → resolução de projeto → plano LLM → autocrítica LLM → decomposição JSON. Detecção de ciclo real (Tarjan-like), rodada 2x (pós-decomposição e pós-dedup) |
| `task_queue.py` | Madura | `get_promotable_tasks` é um filtro puro (não muta estado, não acessa `Store`) que computa elegibilidade por dependência a partir de uma lista snapshot; `materialize_plan` cria Issues reais no GitHub com batching topológico. **Correção**: prioridade por rank+created_at é responsabilidade de `decisions.get_priority_queue()`, não deste módulo |
| `merge_policy.py` | Madura, mas **sem fila real** | Verifica review PASS (task/repo/PR/SHA exatos), PR aberto/não-draft, base/head corretos, checks GitHub verdes; confirma merge via `gh pr view` pós-merge (nunca confia só no exit code). **Não serializa merges concorrentes entre PRs diferentes** |
| `review_pipeline.py` | Madura | Independência de revisor **forçada em código** (`ValueError` se implementador == revisor). Escalonamento 3-falhas → `CEO_ESCALATION_REQUIRED` |
| `persistence.py` | Madura | SQLite WAL, `Store` único por processo, RLock, `run_in_transaction`/`run_sync_once` atômicos entre conexões |
| `audit.py` | Madura | Redação recursiva de chaves tipo token/password/secret em logs |
| `healthcheck.py` | Madura | Probes GitHub/Paperclip, heartbeats, diagnóstico de idle (bloqueado por dependência vs Paperclip fora vs agente pausado) |
| `github_client.py` | Madura | Usa `gh` CLI via subprocess com lista de argumentos (nunca shell=True); rate-limit com cooldown persistido cross-processo; sem PR/merge/labels (isso é `merge_policy.py`) |
| `telegram_bot.py` / `decisions.py` | Madura | Parser de linguagem natural sem slash-commands (`Objetivo:`, `sim`/`nao`, `prioriza`, `ref: <id> <resposta>`); voz via Groq Whisper; sem risco de injeção de comando (nenhum subprocess/eval recebe texto do Telegram) |
| `voice_facade.py` | Existe, **acoplado em processo** | Único ponto de integração sancionado voz↔orchestrator (`handle_status_query`, `handle_report_query`, `handle_control_query`). **Correção pós-review (Codex)**: o `main.py` deste worktree (branch `integration/orchestration`) já importa e chama `voice_facade` de fato (`main.py:52`, chamadas em `:1663`, `:1672`, `:1681`) — não é uma "cópia forkada esquecida", é código real e integrado nesta branch. O gap real é dois: (1) é chamada de função Python síncrona no mesmo processo — sem isolamento de falha, se `orchestrator/` travar/lançar exceção fora do bloco `try/except` de import, pode afetar a voz; (2) **o monólito de voz rodando ao vivo agora** (`Desktop\Jarvis-AI-For-Windows-2026`, branch `main`, PID 10880) é um checkout separado que ainda **não** tem essa wiring — é um gap de deploy/rollout, não de código-fonte inexistente. `handle_control_query` hoje só responde honestamente que não há pause/resume real implementado — não é um facade incompleto por acidente, é escopo ainda não implementado |
| **WORK_PROTOCOL.md** | Documentado, não aplicado em código | Checkpoints por domínio, Review Tasks como Issues GitHub (`type:review`), regra de prioridade P0-P3, regra de ociosidade, lista de Issues SOLO (#11,#30,#35,#41,#45,#46) — **tudo isso é convenção seguida manualmente por Claude/Codex, não enforcement mecânico** |

## 3. Paperclip

App externa real (não é módulo deste repo): pacote npm `paperclipai` v2026.831.1, instalado globalmente, roda como serviço HTTP local em `127.0.0.1:3100` (loopback-only). **Estava PARADO no momento da auditoria** (connection refused). Responsabilidade: dono do conceito de "companies/agents/issues", dispara a execução real do agente de IA (opaco para este repo). O orchestrator nunca faz `subprocess` de `claude`/`codex`/`gemini`/`opencode` diretamente — cria uma "issue" via HTTP no Paperclip e faz polling do status. `agent_policy.py`/`agent_availability.py` decidem qual agente por nome (política local, não redundante com Paperclip); `paperclip_ops.py`/`paperclip_sync.py` adicionam idempotência/resiliência genuínas sobre o HTTP não confiável.

## 4. CLIs instaladas e autenticadas

| CLI | Instalada | Versão | Autenticação |
|---|---|---|---|
| `claude` | Sim | 2.1.260 | `~/.claude/.credentials.json` presente |
| `codex` | Sim | 0.155.0 | logado |
| `gemini` | Sim | 0.60.0 | logado (API key) |
| `opencode` | Sim | 1.18.31 | logado |
| `gh` | Sim | 2.98.0 | logado, usado por `github_client.py` |
| `paperclipai` | Sim | 2026.831.1 | servidor **parado** no momento da auditoria |

## 5. Testes

**775 passed, 4 failed** (suite completa, `python -m pytest tests/`, ~61s). Os 4 testes usam `tmp_path`/`monkeypatch` corretamente (isolamento de storage confirmado — não há contaminação de produção). As 4 falhas são de um único problema real: **os testes de config esperam ambiente "limpo" (`TELEGRAM_BOT_TOKEN` ausente), mas o token real do processo/ambiente do sistema vaza para dentro do teste** — `_reload_with_clean_env` não está isolando completamente o ambiente do processo, só o `.env` do repo. Isso expôs o valor real do token no output do terminal durante esta auditoria (não reproduzido aqui). **Recomendação de segurança: rotacionar esse token do bot do Telegram como precaução.**

## 6. Worktrees

65 worktrees em `C:\Users\syann\Jarvis-Worktrees\` + 1 em `Desktop\`, todos manuais (nomeados `claude-issueNN`/`codex-issueNN`/`claude-fixNN`/`review-issueNN`), todos correspondendo a branches já mergeadas em `integration/orchestration` (confirmado via `git log --graph`). Nenhum é gerenciado programaticamente pelo orchestrator — criação/limpeza é 100% manual hoje.

## 7. Concorrência e locks

**Não existe lock de recurso em runtime.** `SOLO` é uma anotação de política (`agent_policy.py`, `ExecutionMode.SOLO` quando task toca um "hotspot" como `main.py`) documentada em `WORK_PROTOCOL.md` e checada manualmente pelo agente antes de pegar a tarefa — mas **nenhum código no `scheduler.py`/`task_queue.py` impede admissão concorrente de duas tarefas SOLO conflitantes**. O único lock real é o `RLock` do `persistence.py` protegendo acesso ao SQLite (correto, mas é lock de storage, não de recurso/arquivo de negócio).

## 8. Retry / rate limit / backoff

Bem coberto: `github_client.py` calcula deadline de cooldown a partir de `Retry-After`/`X-RateLimit-Reset` (o maior dos dois), persiste cross-processo em `sync_state`, nunca encurta um cooldown existente. `agent_availability.py` rastreia cooldown por agente localmente. `paperclip_ops.py` usa backoff exponencial (`PaperclipSession`) sobre o HTTP do Paperclip.

## 9. Secrets

- Monólito de voz: `config.py` (gitignored) com `apikey` (Gemini), `groq_apikey`, `ewelink_email/password/region`, `argos_ha_url/key`. Consumido por `main.py`, `openaitest.py`, `paperclip_client.py`.
- Orchestrator: `.env` + `orchestrator/config.py` (loader manual, não usa `python-dotenv` pacote) com `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CONTROL_CHAT_ID`, `TELEGRAM_REPORT_CHAT_ID`, `GROQ_API_KEY` (cópia separada, mesma chave conceitual do monólito), `PAPERCLIP_BASE_URL`/`PAPERCLIP_API_TOKEN`. GitHub não usa token de env — delega para sessão do `gh` CLI.
- **Existe um `config.py` duplicado** (mesmos valores, byte-a-byte igual) no repo do orchestrator, também gitignored.
- Nenhum uso de keyring/cofre de SO.

## 11. Achados da review independente (Codex) incorporados

- **Estado de dispatch não é durável**: quando o Paperclip recebe uma task, `orchestrator.py:468/476` só anexa o ID a uma lista de retorno — nunca persiste a transição `READY → IN_PROGRESS`. `paperclip_sync.py:66/68/104` depois pula direto de `READY`/`IN_PROGRESS` para `DONE` quando o Paperclip termina. Isso significa que "quantas tasks estão realmente em andamento agora" não é uma pergunta que o `Store` consegue responder com confiança hoje — afeta relatório (`run_report()`), diagnóstico de idle, recovery pós-crash, e qualquer lock futuro baseado em estado.
- **Review Tasks (Issues GitHub) não são criadas em código**: `WORK_PROTOCOL.md` exige que toda implementação pronta para revisão vire uma Issue real com label `type:review` (§75-83). `review_pipeline.py` registra o veredito da revisão (pass/fail, contagem de falhas) mas não cria nem força a existência dessas Issues — é convenção seguida manualmente por Claude/Codex, não código.
- Nomes de estado corretos no `models.py`: `IN_PROGRESS` e `IN_REVIEW` (não "WORKING"/"REVIEW" — erro corrigido nesta revisão).

## 10. Recuperação / crash

`persistence.py` com SQLite WAL sobrevive restart (schema com `tasks`, `decisions`, `rate_limits`, `sync_state`, `idempotency_keys`, `review_failures`, `audit_log`, `deploy_failure_episodes`). `run_sync_once`/idempotency keys previnem duplicação de ações após restart. `healthcheck.check_idle` diagnostica se um travamento é dependência bloqueada, Paperclip fora do ar, agente pausado, ou inexplicado. Não há testes de "crash no meio do merge" ou "restart durante task em andamento" confirmados na suite.
