# HARNESS_GAP_ANALYSIS.md

Base: [CURRENT_STATE.md](./CURRENT_STATE.md). Cada gap é comparado contra o Prompt Mestre (harness de engenharia autônoma).

## Reclassificação importante

O harness já existe em ~60-70% do escopo. A maior parte do que falta **não é código novo do zero** — é **converter convenção documentada (`WORK_PROTOCOL.md`) em enforcement mecânico**, e **religar duas peças que já existem mas estão desconectadas** (voz ↔ orchestrator; Paperclip ligado).

## Gaps confirmados (por criticidade)

### G1 — CRÍTICO: Paperclip não está rodando
Sem o serviço em `127.0.0.1:3100` ativo, nenhuma task criada pelo orchestrator é executada de fato — ela fica presa em polling eterno. **Ação**: auto-start do Paperclip (serviço/task agendada) + `healthcheck.py` já detecta isso (`check_idle`), mas hoje só diagnostica, não recupera.

### G2 — CRÍTICO: secret vazando em ambiente de teste
4 testes falhando revelam que `TELEGRAM_BOT_TOKEN` real vaza para o processo de teste via ambiente do SO, não só `.env`. **Ação**: corrigir `_reload_with_clean_env` em `test_orchestrator_config.py` pra realmente isolar `os.environ`, e **rotacionar o token do bot do Telegram** como precaução (já pode ter sido exposto em terminal/logs locais).

### G3 — ALTO (reformulado pós-review): acoplamento em processo, e não deployado ao vivo
Correção: `voice_facade.py` **já está** importado e chamado pelo `main.py` real desta branch (`main.py:52,1663,1672,1681`) — não é código morto/esquecido. O gap real é duplo: (a) é chamada de função Python síncrona no mesmo processo — sem isolamento de falha real; um travamento dentro de uma chamada (não só na importação) não está necessariamente protegido; (b) o monólito de voz **rodando ao vivo agora** (`Desktop\Jarvis-AI-For-Windows-2026`, branch `main`) é um checkout separado que ainda não recebeu essa integração — gap de deploy, não de código inexistente. Adicionalmente, `handle_control_query` hoje é honesto que não existe pause/resume real — uma casca HTTP em cima da função não resolve isso sozinha. **Ação**: ver TARGET_ARCHITECTURE — expor `voice_facade` como endpoint HTTP local (status/report primeiro; control fica explicitamente fora de escopo ou ganha implementação real), com `Store` apontando pro mesmo arquivo SQLite configurado explicitamente (não path implícito por checkout), e só depois sincronizar essa integração para o checkout que roda ao vivo.

### G12 — CRÍTICO (novo, achado pela review): sem transição durável READY→IN_PROGRESS
Quando o Paperclip recebe uma task, nada persiste a transição de estado — `orchestrator.py:468/476` só devolve o ID numa lista, e `paperclip_sync.py` pula direto para `DONE` na conclusão. Isso é um bloqueador real para: enforcement de SOLO (G5, não dá pra saber com confiança "isso já está em andamento"), diagnóstico de idle, recovery pós-crash, e relatórios precisos de "o que está rodando agora". **Deve ser corrigido antes de G5.**

### G13 — MÉDIO (novo, achado pela review): Review Tasks não são criadas em código
`WORK_PROTOCOL.md` exige Issue real (`type:review`) para toda implementação pronta pra revisão. `review_pipeline.py` registra veredito mas não cria/força essas Issues — é convenção manual, não mecanismo. Gap distinto de "independência de revisor" (que já é código, não convenção).

### G4 — ALTO: sem merge queue real
`merge_policy.py` valida cada PR individualmente, mas nada serializa merges concorrentes entre PRs diferentes, nem garante rebase/sync antes de cada merge. Hoje isso não quebra nada porque merges são manuais/um de cada vez na prática, mas é o requisito P22/P23 do Prompt Mestre para autonomia real.

### G5 — MÉDIO: SOLO é convenção, não lock
`ExecutionMode.SOLO` é calculado (`agent_policy.py`) mas nunca consultado por `scheduler.py`/`task_queue.py` para bloquear admissão concorrente. Hoje depende de um agente (Claude/Codex) ler `WORK_PROTOCOL.md` e se autodisciplinar. Risco real: dois agentes editando `main.py`/área hotspot ao mesmo tempo se o protocolo for esquecido.

### G6 — MÉDIO: dependência via LLM, não GitHub nativo
`planner.py` extrai dependências do campo `depends_on_index` que o próprio LLM gera durante a decomposição — não de labels/campos estruturados do GitHub Issue. Funciona (com detecção de ciclo real), mas significa que a fonte de verdade do grafo de dependências vive dentro do texto gerado por LLM, não é diretamente inspecionável/editável via GitHub UI.

### G7 — MÉDIO: 65 worktrees órfãos
Todos correspondem a branches já mergeadas — limpeza seria puro ganho (espaço em disco, clareza), zero risco confirmado via `git log --graph`.

### G8 — MÉDIO: secrets duplicados, sem cofre
`config.py` idêntico existe em dois lugares (monólito + orchestrator), ambos gitignored mas em texto plano em disco. Sem keyring/vault. Baixo risco imediato (não commitado), mas viola boa prática e dificulta rotação (precisa atualizar em 2 lugares).

### G9 — BAIXO: sem auto-start / Windows service
Hoje inicia via `.bat` manual. Sem restart automático em crash do processo host (diferente de retry de tasks, que já existe internamente).

### G10 — BAIXO: sem memória de "lessons" cross-sessão
Existe `jarvis_memory.json` (conversa) e `audit_log` (ações), mas nada equivalente a uma base de "lesson learned → guardrail" persistente e pesquisável entre sessões de implementação.

### G11 — BAIXO: CONTEXT.md desatualizado
Documentação própria do projeto está ~4 dias/várias PRs atrás do HEAD real — não é um gap funcional, mas reduz confiabilidade de "ground truth" para quem (humano ou agente) ler primeiro.

## O que NÃO é gap (confirmar para não desperdiçar trabalho)

- Dependency graph com detecção de ciclo: **já existe e é robusto** (rodado 2x, inclusive pós-dedup).
- Independência de revisor: **já é forçada em código**, não só convenção.
- Idempotência de criação de Issue/task: **já implementada** (correlation_id em múltiplas camadas).
- Rate-limit/backoff do GitHub: **já implementado e persistido cross-processo**.
- Redação de secrets em logs: **já implementada** (`audit.py`).
- Isolamento de teste (storage): **já correto** (`tmp_path`, `monkeypatch` em toda a suite, exceto o vazamento de env do G2).
- Comando via Telegram sem risco de injeção: **confirmado seguro**.
