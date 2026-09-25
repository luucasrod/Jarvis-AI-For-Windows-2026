# IMPLEMENTATION_PLAN.md

Segue `WORK_PROTOCOL.md` já existente: cada item vira Issue no GitHub, branch própria, worktree próprio, PR próprio, review cruzado (quem implementa não revisa), checkpoint ativo = `integration/orchestration`. Nenhuma fase toca `main` do monólito de voz nem os processos ao vivo.

## Fase 0 — Auditoria e planejamento (ESTA SESSÃO)
- [x] Auditoria profunda (5 agentes paralelos + verificação direta)
- [x] Worktree isolado `harness/audit-and-plan` criado a partir de `integration/orchestration`
- [x] CURRENT_STATE.md, HARNESS_GAP_ANALYSIS.md, TARGET_ARCHITECTURE.md, IMPLEMENTATION_PLAN.md
- [ ] Review crítica independente destes 4 documentos (outro agente/CLI)
- [ ] Incorporar correções válidas da review
- [ ] Checkpoint: commit destes docs na branch `harness/audit-and-plan`, abrir PR

## Fase 1 — Correções de baixo risco, alto valor (sem dependências entre si → paralelizável)
1. **G2 — Corrigir vazamento de env em teste**: `_reload_with_clean_env` em `test_orchestrator_config.py` deve limpar `os.environ` de fato. Critério de aceite: os 4 testes falhando passam a passar sem depender do ambiente do processo. Issue SOLO=não.
2. **G7 — Limpeza de worktrees órfãos**: script determinístico que verifica merge status antes de remover; primeira execução com relatório dry-run revisado por humano (já confirmamos hoje que os 65 atuais são seguros). Issue SOLO=não.
3. **G11 — Atualizar CONTEXT.md**: refletir HEAD atual (`866c582`) em vez do snapshot de 4 dias atrás. Issue SOLO=não.
4. **Ação humana (fora do harness, registrar como NEEDS_HUMAN)**: rotacionar token do bot do Telegram exposto durante a auditoria.

Cada uma é uma Issue independente, pode rodar em paralelo (não tocam os mesmos arquivos). Testes + review cruzado + merge no checkpoint `integration/orchestration` antes de prosseguir.

## Fase 2 — Paperclip operacional (resolve G1)
1. Auto-start do serviço Paperclip (Task Scheduler ou serviço Windows) antes do `orchestrator.runtime`.
2. Estender `healthcheck.check_idle` para tentar reiniciar Paperclip automaticamente antes de escalar `NEEDS_HUMAN`.
Critério de aceite: matar o processo Paperclip manualmente em ambiente de teste e confirmar que o healthcheck detecta e reinicia dentro de N ciclos, sem intervenção humana.

## Fase 3 — Fronteira Voz ↔ Orchestrator (resolve G3, SOLO — toca `main.py`)
1. Servidor HTTP local fino em cima de `voice_facade.py` (3 endpoints: status/report/control).
2. Cliente HTTP no `main.py` **real** do monólito (timeout curto, fallback gracioso).
3. Testar com orchestrator desligado (voz deve continuar funcionando) e com voz desligada (orchestrator não deve notar diferença).
4. Descontinuar a cópia forkada de `main.py` dentro do repo do orchestrator.
Esta é SOLO (toca `main.py` do monólito) — seguir regra do WORK_PROTOCOL.md, garantir que nenhuma outra tarefa SOLO está em andamento antes de começar.

## Fase 4 — Merge queue real (resolve G4)
1. Tabela `merge_queue` em `persistence.py` (mesmo padrão WAL/RLock já usado).
2. `merge_policy.py` consulta a fila antes de chamar `gh pr merge`; sync/rebase check entre merges sucessivos do mesmo repo.
3. **Testes obrigatórios antes de qualquer ativação de auto-merge automático amplo**:
   - Dois PRs prontos simultaneamente no mesmo repo → confirmar serialização (nunca merge simultâneo).
   - PR fica desatualizado por causa do merge anterior → confirmar detecção e re-sync antes de mergear.
   - Simular falha no meio do merge (kill do processo) → confirmar que o estado persistido permite retomar sem merge duplicado nem PR perdido.
   - Confirmar que branch protection real do GitHub (configuração de repo, não só código) impede push direto de um worker à `main`/`integration/orchestration`.
   - Confirmar que nenhum módulo permite um "worker" chamar merge sem passar por `review_pipeline` PASS + fila.

## Fase 5 — Enforcement de SOLO (resolve G5)
1. `task_queue.get_promotable_tasks` ganha check de conflito SOLO por projeto (não promove 2 SOLO simultâneas no mesmo projeto).
2. Teste: duas tasks SOLO do mesmo projeto, confirmar que só uma é promovida por vez.

## Fase 6 — Consolidação de secrets (resolve G8)
1. Fonte única de `config.py`/`.env`, cópia do orchestrator lê da fonte única.
2. Confirmar que os processos ao vivo (voz + orchestrator) continuam funcionando após a migração (rodar em paralelo antes de remover a leitura antiga, exatamente como instruído).

## Gate de ativação de auto-merge automático (só depois de Fases 1-4 validadas)
Auto-merge **já está implementado desde `merge_policy.py`** (existia antes desta auditoria). O que muda é a confiança para deixá-lo rodar sem supervisão:
- [ ] Merge queue (Fase 4) testada em worktree/sandbox conforme critérios acima
- [ ] Rollback/recovery testado (merge falho não deixa estado inconsistente)
- [ ] Branch protection do GitHub confirmada nas configurações do repo (não assumir, verificar via `gh api`)
- [ ] Confirmado que nenhum worker individual consegue mergear sem review PASS + fila
- [ ] Rodar um ciclo real ponta-a-ponta em Issue de baixo risco (ex. uma das Issues da Fase 1) observando o fluxo completo antes de considerar "normal operacional"

Depois disso, auto-merge roda no fluxo normal sem aprovação individual por PR — exatamente como definido pelo usuário. Os Human Approval Gates (dados destrutivos, secrets, billing, migrations irreversíveis) continuam valendo independentemente desta fase.

## Ordem de execução recomendada
Fase 1 (paralelo) → Fase 2 → Fase 3 (SOLO, isolada) → Fase 4 → gate de auto-merge → Fase 5 → Fase 6. Fases 2, 5 e 6 não têm dependência forte entre si e podem ser reordenadas conforme disponibilidade de agentes.
