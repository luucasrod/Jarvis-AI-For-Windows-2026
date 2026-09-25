# IMPLEMENTATION_PLAN.md

**v2 — revisado após review crítica independente (Codex), ver REVIEW.md.**

Segue `WORK_PROTOCOL.md` já existente: cada item vira Issue no GitHub, branch própria, worktree próprio, PR próprio, review cruzado (quem implementa não revisa), checkpoint ativo = `integration/orchestration`. Nenhuma fase toca `main` do monólito de voz nem os processos ao vivo.

**Mudança de ordem pós-review**: a v1 colocava o gate de auto-merge antes do enforcement de SOLO — invertido, porque ativar merge automático amplo antes de serializar recursos aumenta risco exatamente na área que o plano já reconhece como sensível. Também foi inserida uma fase nova (estado de dispatch durável) como pré-requisito real de SOLO, que a v1 não via.

## Fase 0 — Auditoria e planejamento (CONCLUÍDA)
- [x] Auditoria profunda (5 agentes paralelos + verificação direta)
- [x] Worktree isolado `harness/audit-and-plan` criado a partir de `integration/orchestration`
- [x] CURRENT_STATE.md, HARNESS_GAP_ANALYSIS.md, TARGET_ARCHITECTURE.md, IMPLEMENTATION_PLAN.md
- [x] Review crítica independente (Codex) — REVIEW.md, 6 correções + 6 gaps novos incorporados
- [ ] Checkpoint: PR desta branch para `integration/orchestration`

## Fase 1 — Correções de baixo risco, alto valor (paralelizável, sem dependência entre si)
1. **G2 — Corrigir vazamento de env em teste**: `_reload_with_clean_env` em `test_orchestrator_config.py` deve limpar `os.environ` de fato. Critério de aceite: os 4 testes falhando passam.
2. **G7 — Limpeza de worktrees órfãos**: script determinístico com dry-run revisado antes de remover (65 atuais já confirmados seguros).
3. **G11 — Atualizar CONTEXT.md**: refletir HEAD atual (`866c582`).
4. **Ação humana (NEEDS_HUMAN, fora do harness)**: rotacionar token do bot do Telegram exposto durante a auditoria.

## Fase 2 — Estado de dispatch durável (resolve G12 — pré-requisito de Fase 3)
1. No ponto em que `create_task_idempotent` confirma aceite pelo Paperclip, persistir `READY -> IN_PROGRESS` via `run_in_transaction`.
2. Teste de crash/restart específico deste ponto (não só merge): task criada/atribuída, processo reinicia com estado ainda `READY` — confirmar que o dispatcher não duplica a atribuição nem perde o registro de que já está em andamento.
3. Critério de aceite: `run_report()` reflete corretamente quantas tasks estão `IN_PROGRESS` em qualquer momento, incluindo após restart.

## Fase 3 — Enforcement de SOLO (resolve G5, depende de Fase 2)
1. Check de conflito SOLO entra no admission/dispatch real (`Scheduler.admit_task`/`reconsider`), não em `task_queue.get_promotable_tasks` (que é filtro puro).
2. Consulta usa os nomes corretos de estado (`IN_PROGRESS`, `IN_REVIEW`).
3. Teste: duas tasks SOLO do mesmo projeto — confirmar que só uma é despachada por vez.

## Fase 4 — Paperclip operacional (resolve G1)
1. Auto-start do serviço Paperclip (Task Scheduler/serviço Windows) antes do `orchestrator.runtime`.
2. Estender `healthcheck.check_idle` para tentar reiniciar Paperclip automaticamente antes de escalar `NEEDS_HUMAN`.
3. Teste: matar o processo Paperclip manualmente, confirmar detecção + restart automático dentro de N ciclos.

## Fase 5 — Fronteira Voz ↔ Orchestrator (resolve G3 — SOLO, toca `main.py`)
1. Servidor HTTP local fino sobre `voice_facade.py`: `GET /voice/status`, `GET /voice/report`. `control` fica **fora de escopo** nesta fase (não existe pause/resume real hoje — não fingir que existe via HTTP).
2. `Store` do servidor HTTP aponta para path de SQLite configurado explicitamente (env var), não default implícito por checkout.
3. Trocar a chamada síncrona em processo no `main.py` desta branch por chamada HTTP com timeout curto (~2s) e fallback gracioso.
4. **Sincronizar essa integração para o checkout ao vivo** (`Desktop\Jarvis-AI-For-Windows-2026`, branch `main`) — hoje esse checkout não tem nenhuma referência a `orchestrator`/`voice_facade`.
5. Testar com orchestrator desligado (voz continua) e voz desligada (orchestrator não nota diferença).
6. Esta é SOLO (toca `main.py`) — confirmar via `WORK_PROTOCOL.md` que nenhuma outra SOLO está em andamento antes de começar.

## Fase 6 — Merge queue real (resolve G4)
1. Tabela `merge_queue` com schema completo: `pr_id, repo, task_id, expected_head_sha, expected_base, required_checks_snapshot, requested_at, status, lease_expires_at, attempt` (schema v1 era subespecificado — corrigido pós-review).
2. `merge_policy.py` consulta a fila antes de `gh pr merge`; lease `MERGING` expirada é liberada e reavaliada do zero (re-checar SHA atual).
3. **Testes obrigatórios antes do gate de auto-merge**:
   - Dois PRs prontos simultaneamente no mesmo repo → confirmar serialização.
   - PR desatualizado pelo merge anterior → confirmar detecção + re-sync antes de mergear.
   - Kill do processo no meio do merge → confirmar retomada sem merge duplicado nem PR perdido (lease expira, reavalia).
   - Confirmar branch protection real do GitHub via `gh api` (configuração de repo, não assumir).
   - Confirmar que nenhum worker consegue mergear sem passar por `review_pipeline` PASS + fila.

## Fase 7 — Review Tasks como Issues reais (resolve G13)
1. `request_review` passa a criar (idempotente) uma Issue GitHub `type:review` referenciando Issue original/PR/critérios, fechando o gap convenção-vs-código do `WORK_PROTOCOL.md` §3.

## Gate de ativação de auto-merge automático (só depois das Fases 2, 3 e 6 validadas)
Auto-merge **já existe em código** (`merge_policy.py`, anterior a esta auditoria). O que muda é a confiança pra deixá-lo rodar sem supervisão — por isso o gate agora vem **depois** de SOLO (Fase 3), não antes:
- [ ] Estado de dispatch durável (Fase 2) validado
- [ ] Enforcement de SOLO (Fase 3) validado
- [ ] Merge queue (Fase 6) testada conforme critérios acima
- [ ] Rollback/recovery confirmado (merge falho não deixa estado inconsistente)
- [ ] Branch protection do GitHub confirmada nas configurações reais do repo
- [ ] Ciclo real ponta-a-ponta numa Issue de baixo risco (ex. uma da Fase 1) observado antes de considerar "normal operacional"

Depois disso, auto-merge roda no fluxo normal sem aprovação individual por PR. Os Human Approval Gates (dados destrutivos, secrets, billing, migrations irreversíveis) continuam valendo independentemente desta fase.

## Fase 8 — Consolidação de secrets (resolve G2 parte estrutural, G8)
1. Fonte única de `config.py`/`.env`; cópia do orchestrator lê da fonte única em vez de duplicar.
2. Confirmar que os processos ao vivo continuam funcionando após a migração antes de remover a leitura antiga.

## Ordem de execução
Fase 1 (paralelo) → Fase 2 → Fase 3 → Fase 4 (paralelo com 3 se recursos permitirem) → Fase 5 (SOLO, isolada) → Fase 6 → **gate de auto-merge** → Fase 7 (paralelo com 6) → Fase 8.
