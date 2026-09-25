# TARGET_ARCHITECTURE.md

Princípio: **estender, não recriar**. Todo módulo listado em CURRENT_STATE §2 permanece como está, salvo mudança pontual explicitamente descrita abaixo.

## 1. Fronteira Voz ↔ Orchestrator (resolve G3)

```
Jarvis Voice (main.py, processo A)          Jarvis Orchestrator (processo B, já rodando)
     |                                              |
     |  pergunta de status/report/controle          |
     v                                              v
 HTTP GET localhost:<porta>/voice/status  ---->  voice_facade.py exposto via
 HTTP GET localhost:<porta>/voice/report          um servidor HTTP local mínimo
 HTTP GET localhost:<porta>/voice/control          (stdlib http.server ou Flask
                                                      já disponível — decidir na
                                                      Fase 1 pelo que tiver menor
                                                      footprint de dependência)
```

- `voice_facade.py` **não muda sua lógica interna** (já lê `Store`/`history`/Paperclip corretamente) — só ganha uma casca HTTP fina em cima das 3 funções já existentes.
- `main.py` real (monólito, não a cópia forkada) ganha um cliente HTTP pequeno com **timeout curto (ex. 2s) e fallback gracioso** ("não consegui checar o status agora") — nunca trava a voz esperando o orchestrator.
- Se o orchestrator estiver fora do ar: voz responde com fallback, não quebra.
- Se a voz estiver fora do ar: orchestrator não depende dela para nada (já confirmado hoje).
- Consolidação de código: a cópia forkada de `main.py` dentro do repo do orchestrator é descontinuada depois que o real `main.py` ganhar o cliente HTTP — evita duas versões divergentes do mesmo arquivo permanentemente.

## 2. Paperclip (resolve G1)

Decisão: **manter Paperclip como executor real dos agentes** — a auditoria confirmou que a divisão de responsabilidade é limpa (Jarvis decide política, Paperclip executa) e não há redundância a eliminar. Mudança necessária é só operacional:
- Auto-start do serviço Paperclip (Windows: Task Scheduler ou nssm/serviço, iniciando antes do `orchestrator.runtime`).
- `healthcheck.py` já detecta Paperclip fora do ar (`check_idle`) — estender para **tentar reiniciar automaticameante** (watchdog, não só diagnosticar) antes de escalar para `NEEDS_HUMAN`.

Isso mantém o princípio "Jarvis chama diretamente os CLIs quando apropriado" como uma melhoria futura opcional (Paperclip já abstrai isso de forma que funciona), não uma reescrita obrigatória agora.

## 3. Merge Queue real (resolve G4)

Adicionar uma tabela SQLite `merge_queue` (mesmo padrão de `persistence.py`, WAL+RLock já existentes):

```
merge_queue(pr_id, repo, requested_at, status[QUEUED|MERGING|DONE|FAILED], attempt)
```

Fluxo: `merge_policy.py` continua validando cada PR individualmente (nenhuma mudança na lógica de validação); antes de chamar `gh pr merge`, o PR precisa estar no topo da fila E nenhum outro merge em `status=MERGING` para o mesmo repo. Após merge: sync/rebase check nas próximas entradas da fila (uma branch pode ter ficado desatualizada pelo merge anterior) antes de prosseguir. Serializado por repo, não globalmente (merges em repos diferentes podem ocorrer em paralelo).

## 4. Enforcement de SOLO (resolve G5)

`task_queue.get_promotable_tasks` ganha um check adicional: antes de promover uma task com `execution_mode == SOLO`, consultar se existe outra task com `state IN (WORKING, REVIEW)` que também seja SOLO **para o mesmo projeto** — se sim, a nova fica em espera (não fica bloqueada permanentemente, só não promove neste ciclo). Implementação simples porque `persistence.py` já tem tudo que é necessário para essa consulta (tabela `tasks` com `state`).

## 5. Secrets (resolve G2, G8)

- Curto prazo (sem quebrar nada rodando): consolidar as 2 cópias de `config.py` em uma única fonte, com a cópia do orchestrator passando a **ler** da fonte única via caminho relativo/env var explícita, em vez de manter arquivo duplicado.
- Corrigir `_reload_with_clean_env` em `test_orchestrator_config.py` para limpar `os.environ` de fato (não só simular via `.env` vazio) antes de cada teste de config — elimina o vazamento do G2.
- **Ação humana necessária (fora do harness)**: rotacionar o token do bot do Telegram exposto durante a auditoria.
- Não migrar para keyring/vault agora — fora de escopo da Fase 1, registrar como débito técnico documentado (não é bloqueio de segurança crítico: arquivo já é local, gitignored, sem exposição em git).

## 6. Dependency graph (G6 — aceitar, não migrar agora)

Manter a extração via LLM (`depends_on_index`) — já funciona com detecção de ciclo robusta. Migrar para labels/campos nativos do GitHub seria uma reescrita grande de `planner.py`/`task_queue.py` para um ganho principalmente de "inspecionabilidade humana via GitHub UI", não de correção. Registrar como melhoria futura opcional, não Fase 1/2.

## 7. Limpeza de worktrees (G7)

Script determinístico (Python, chamado pelo orchestrator ou rodado manualmente): para cada worktree em `Jarvis-Worktrees/`, verificar se a branch correspondente já está mergeada em `integration/orchestration` (`git branch --merged`); se sim, `git worktree remove` + `git branch -d`. Rodar como parte do `healthcheck.py` (dry-run report primeiro, remoção só após confirmação — dado que já confirmamos hoje que os 65 atuais são seguros de remover, a primeira rodada pode ser manual).

## 8. O que fica fora do escopo desta fase (débito técnico registrado, não esquecido)

- Auto-start via Windows Service para o próprio orchestrator/voz (G9).
- Memória de lessons cross-sessão estruturada (G10) — WORK_PROTOCOL.md e docs/ai/* já cumprem parte desse papel para humanos/agentes lendo o repo; formalizar como base pesquisável é melhoria de Fase posterior.
- Atualização de CONTEXT.md para refletir HEAD atual (G11) — baixo risco, fazer como parte do checkpoint da Fase 1.

## 9. Guardrail de branch/produção

Nenhuma mudança de Fase 1/2 toca `main` do monólito de voz nem os processos `pythonw.exe`/`orchestrator.runtime` já rodando. Todo trabalho acontece em `harness/audit-and-plan` (este worktree) e branches de Issue subsequentes, seguindo exatamente `WORK_PROTOCOL.md` (checkpoint ativo = `integration/orchestration`, PR por Issue, review cruzado, merge só após PASS).
