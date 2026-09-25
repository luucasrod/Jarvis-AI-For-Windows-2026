# TARGET_ARCHITECTURE.md

Princípio: **estender, não recriar**. Todo módulo listado em CURRENT_STATE §2 permanece como está, salvo mudança pontual explicitamente descrita abaixo.

## 1. Fronteira Voz ↔ Orchestrator (resolve G3)

```
Jarvis Voice (main.py, processo A)          Jarvis Orchestrator (processo B, já rodando)
     |                                              |
     |  pergunta de status/report/controle          |
     v                                              v
 HTTP GET  localhost:<porta>/voice/status   ---->  voice_facade.py exposto via
 HTTP GET  localhost:<porta>/voice/report          um servidor HTTP local mínimo
 HTTP POST localhost:<porta>/voice/control          (stdlib http.server ou Flask
                                                      já disponível — decidir na
                                                      Fase implementação pelo que
                                                      tiver menor footprint)
```

**Correções pós-review**:
- `control` usa **POST**, não GET — GET não deve ter efeito colateral (retry/prefetch/crawler não podem disparar mudança de estado). Bind em loopback + token local compartilhado ou checagem de confiança do processo.
- `handle_control_query` hoje é honesto que **não existe pause/resume real** — a casca HTTP não resolve isso sozinha. Decisão explícita necessária: ou (a) escopo desta fase é só `status`+`report`, `control` fica fora até ter semântica real implementada, ou (b) implementar pause/resume de verdade antes de expor o endpoint. Recomendação: (a) para não inflar escopo.
- `Store` do lado HTTP deve apontar para um **path de SQLite configurado explicitamente** (env var, não default implícito por checkout) — hoje `Store()` deriva o path do checkout que contém `orchestrator/persistence.py`; se voz e orchestrator rodarem de checkouts diferentes, podem silenciosamente ler arquivos `.db` diferentes sem erro nenhum.
- `voice_facade.py` **não muda sua lógica interna** — só ganha uma casca HTTP fina em cima das funções já existentes.
- **Correção de fato**: o `main.py` desta branch (`integration/orchestration`) **já importa e chama** `voice_facade` em processo (`main.py:52,1663,1672,1681`) — não existe "cópia forkada" a descontinuar, é código real. O trabalho aqui é (1) trocar a chamada síncrona em processo por chamada HTTP com timeout curto (ex. 2s) e fallback gracioso, e (2) **sincronizar essa integração para o checkout que roda ao vivo hoje** (`Desktop\Jarvis-AI-For-Windows-2026`, branch `main`), que ainda não tem nenhuma referência a `orchestrator`/`voice_facade` — isso é um gap de deploy, não de merge de arquivo duplicado.
- Se o orchestrator estiver fora do ar: voz responde com fallback, não quebra.
- Se a voz estiver fora do ar: orchestrator não depende dela para nada (já confirmado hoje).

## 2. Paperclip (resolve G1)

Decisão: **manter Paperclip como executor real dos agentes** — a auditoria confirmou que a divisão de responsabilidade é limpa (Jarvis decide política, Paperclip executa) e não há redundância a eliminar. Mudança necessária é só operacional:
- Auto-start do serviço Paperclip (Windows: Task Scheduler ou nssm/serviço, iniciando antes do `orchestrator.runtime`).
- `healthcheck.py` já detecta Paperclip fora do ar (`check_idle`) — estender para **tentar reiniciar automaticameante** (watchdog, não só diagnosticar) antes de escalar para `NEEDS_HUMAN`.

Isso mantém o princípio "Jarvis chama diretamente os CLIs quando apropriado" como uma melhoria futura opcional (Paperclip já abstrai isso de forma que funciona), não uma reescrita obrigatória agora.

## 3. Merge Queue real (resolve G4)

Adicionar uma tabela SQLite `merge_queue` (mesmo padrão de `persistence.py`, WAL+RLock já existentes). **Schema corrigido pós-review** (o original era subespecificado — só `pr_id` não basta, porque `merge_policy.py` já é estrito sobre task/repo/PR/SHA/base e a fila precisa preservar exatamente essas invariantes):

```
merge_queue(
  pr_id, repo, task_id,
  expected_head_sha, expected_base,
  required_checks_snapshot,     -- política de checks obrigatórios no momento do enfileiramento
  requested_at, status[QUEUED|MERGING|DONE|FAILED],
  lease_expires_at,             -- para detectar e liberar lease MERGING travada (crash no meio do merge)
  attempt
)
```

Fluxo: `merge_policy.py` continua validando cada PR individualmente (nenhuma mudança na lógica de validação); antes de chamar `gh pr merge`, o PR precisa estar no topo da fila E nenhum outro merge em `status=MERGING` não-expirado para o mesmo repo. Após merge: sync/rebase check nas próximas entradas da fila (uma branch pode ter ficado desatualizada pelo merge anterior) antes de prosseguir. Serializado por repo, não globalmente. Uma lease `MERGING` expirada (processo morreu no meio) é liberada e reavaliada do zero (re-checar head SHA atual, não assumir que nada mudou).

## 4. Estado de dispatch durável (resolve G12 — pré-requisito de G5)

Antes de qualquer lock de SOLO fazer sentido, o dispatch precisa persistir a transição real de estado. Hoje `orchestrator.py` (linhas ~468/476) só devolve o ID da task atribuída ao Paperclip numa lista — nunca escreve `IN_PROGRESS` no `Store`. Correção: no ponto exato em que uma task é confirmada como aceita pelo Paperclip (`create_task_idempotent` retorna sucesso), persistir a transição `READY -> IN_PROGRESS` via `run_in_transaction` (já existe em `persistence.py`). `paperclip_sync.py` continua responsável por `IN_PROGRESS -> DONE` na conclusão — sem mudança aí.

## 5. Enforcement de SOLO (resolve G5, depende de G12)

**Localização corrigida pós-review**: `task_queue.get_promotable_tasks` é um filtro puro (recebe snapshot, não acessa `Store`, não muta nada) — não é o lugar certo para um lock. O caminho real de admissão/dispatch é `run_daily_cycle() -> decisions.get_priority_queue() -> Scheduler.admit_task()/reconsider() -> dispatch`. O check de conflito SOLO entra no admission/dispatch (`Scheduler.admit_task`/`reconsider` ou um novo passo de reserva transacional), consultando o `Store` (agora que G12 garante que `IN_PROGRESS` é confiável): antes de despachar uma task `execution_mode == SOLO`, verificar se já existe outra task do mesmo projeto com estado `IN_PROGRESS` ou `IN_REVIEW` (nomes corretos do enum em `models.py` — não "WORKING"/"REVIEW") que também seja SOLO. Se sim, a nova fica em espera (não bloqueada permanentemente, só não é despachada neste ciclo).

## 6. Secrets (resolve G2, G8)

- Curto prazo (sem quebrar nada rodando): consolidar as 2 cópias de `config.py` em uma única fonte, com a cópia do orchestrator passando a **ler** da fonte única via caminho relativo/env var explícita, em vez de manter arquivo duplicado.
- Corrigir `_reload_with_clean_env` em `test_orchestrator_config.py` para limpar `os.environ` de fato (não só simular via `.env` vazio) antes de cada teste de config — elimina o vazamento do G2.
- **Ação humana necessária (fora do harness)**: rotacionar o token do bot do Telegram exposto durante a auditoria.
- Não migrar para keyring/vault agora — fora de escopo da Fase 1, registrar como débito técnico documentado (não é bloqueio de segurança crítico: arquivo já é local, gitignored, sem exposição em git).

## 7. Dependency graph (G6 — aceitar, não migrar agora)

Manter a extração via LLM (`depends_on_index`) — já funciona com detecção de ciclo robusta. Migrar para labels/campos nativos do GitHub seria uma reescrita grande de `planner.py`/`task_queue.py` para um ganho principalmente de "inspecionabilidade humana via GitHub UI", não de correção. Registrar como melhoria futura opcional, não Fase 1/2.

## 8. Limpeza de worktrees (G7)

Script determinístico (Python, chamado pelo orchestrator ou rodado manualmente): para cada worktree em `Jarvis-Worktrees/`, verificar se a branch correspondente já está mergeada em `integration/orchestration` (`git branch --merged`); se sim, `git worktree remove` + `git branch -d`. Rodar como parte do `healthcheck.py` (dry-run report primeiro, remoção só após confirmação — dado que já confirmamos hoje que os 65 atuais são seguros de remover, a primeira rodada pode ser manual).

## 9. Review Tasks como Issues reais (resolve G13)

`review_pipeline.request_review`/`record_review_result` continuam sendo a lógica de veredito (sem mudança). Adicionar: ao chamar `request_review`, criar (idempotente, seguindo o mesmo padrão de correlation_id de `github_client.create_issue`) uma Issue GitHub com label `type:review` referenciando Issue original/PR/implementador/critérios — exatamente como `WORK_PROTOCOL.md` §3 já exige manualmente. Isso fecha o gap convenção-vs-código sem duplicar a lógica de veredito existente.

## 10. O que fica fora do escopo desta fase (débito técnico registrado, não esquecido)

- Auto-start via Windows Service para o próprio orchestrator/voz (G9).
- Memória de lessons cross-sessão estruturada (G10) — WORK_PROTOCOL.md e docs/ai/* já cumprem parte desse papel para humanos/agentes lendo o repo; formalizar como base pesquisável é melhoria de Fase posterior.
- Atualização de CONTEXT.md para refletir HEAD atual (G11) — baixo risco, fazer como parte do checkpoint da Fase 1.

## 11. Guardrail de branch/produção

Nenhuma mudança de Fase 1/2 toca `main` do monólito de voz nem os processos `pythonw.exe`/`orchestrator.runtime` já rodando. Todo trabalho acontece em `harness/audit-and-plan` (este worktree) e branches de Issue subsequentes, seguindo exatamente `WORK_PROTOCOL.md` (checkpoint ativo = `integration/orchestration`, PR por Issue, review cruzado, merge só após PASS).
