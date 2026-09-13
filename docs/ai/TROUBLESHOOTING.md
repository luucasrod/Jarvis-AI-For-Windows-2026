# Diagnostico do orquestrador

Referencia: checkpoint `e23a74e`, 2026-09-13, issue #43. Comece por
[CONTEXT.md](CONTEXT.md): nesta base o ciclo diario completo nao esta conectado
a voz. Processo Python vivo ou modulo importavel nao prova execucao de tarefa.

## Paperclip offline ou sem progresso

Confira PAPERCLIP_BASE_URL, servidor e PAPERCLIP_TIMEOUT_SECONDS. Para servidor
autenticado, confira localmente a presenca de PAPERCLIP_API_TOKEN, sem publicar
seu valor. Use `paperclip_client.py`; nao altere node_modules para diagnosticar.

PaperclipSession retorna available=False e motivo. Durante backoff,
retry_after_seconds informa espera restante sem rede. Reutilize a sessao:
recria-la a cada tick elimina a protecao contra rajadas. O consumidor precisa
tentar depois do prazo; nao ha thread interna. Cooldown e em memoria, mas as
claims de criacao ficam no Store.

uncertain=True pode indicar criacao remota cuja resposta se perdeu. Preserve
banco e correlation_id para reconciliar; nao apague claim nem invente nova
identidade para reenviar. Criacao available=True nao comprova agente atribuido
ou progresso: consulte status e identidade do agente. Pausa deliberada ou de
orcamento explica ociosidade; nao a remova automaticamente.

detect_restart exige serverInfo.processStartedAt valido. Primeira leitura
estabelece referencia e retorna False; falha de health ou mudanca de versao
nao prova restart. Veja [PAPERCLIP_RESILIENCE.md](PAPERCLIP_RESILIENCE.md) e
[PAPERCLIP_OPS.md](PAPERCLIP_OPS.md).

## GitHub indisponivel, sem permissao ou limitado

Verifique a CLI no mesmo usuario que executa o processo:

```powershell
gh auth status
gh repo view luucasrod/Jarvis-AI-For-Windows-2026 --json nameWithOwner
```

Esses comandos nao criam trabalho remoto. Nao use gh auth token nem copie
credenciais para logs. Autenticacao nao garante escrita em todo repositorio.

HTTP 429 ou 403 com indicacao de quota persiste espera compartilhada por
host/banco. Retry-After e reset do servidor sao respeitados; reiniciar nao
encurta o prazo. 403 sem indicacao de quota pode ser permissao/autenticacao.
Veja [GITHUB_RATE_LIMITS.md](GITHUB_RATE_LIMITS.md).

`reprocess_pending_ops(store, limit=20)` executa uma rodada de operacoes vencidas,
nao instala retry periodico. Lease vigente, prazo futuro ou resultado incerto
podem adiar conclusao. Nao zere leases/next_attempt para forcar POST. failed
exige diagnostico; done nao deve ser reenviado. Reconciliacao usa marcador e
listagem completa, inclusive fechadas; pagina incompleta nao prova ausencia.

Se existe Issue mas falta BLOCKS, `list_incomplete_relationships(store, repo=...)`
mostra delta/motivo pendente. Preserve corpo remoto ao reexecutar reparo pelo
fluxo apropriado; a consulta so lista, nao corrige. Veja [GITHUB_CLIENT.md](GITHUB_CLIENT.md).

## Telegram sem credenciais ou com entrega incerta

Confira localmente TELEGRAM_BOT_TOKEN, TELEGRAM_CONTROL_CHAT_ID e
TELEGRAM_REPORT_CHAT_ID. Os chats tem papeis distintos. validate_config informa
nomes ausentes sem valores. O .env do pacote nao preenche chaves de voz do
config.py da raiz. Reinicie o consumidor para recarregar mudancas do .env,
lido durante importacao.

token invalido corresponde a 401. chat_id invalido ou mensagem rejeitada vem
de 400 e nao identifica sozinho a causa. Timeout, rede ou JSON sem confirmacao
nao comprovam ausencia de entrega. Health unknown pode significar nenhum envio
observado no canal; sondas nao enviam mensagem de teste automaticamente.

**Lacuna nesta base (#133):** create_pending_decision grava chave depois do
envio. Crash nessa janela, resposta perdida e concorrencia podem duplicar
mensagens. Nao trate retry como garantidamente seguro nem registre entrega
incerta como sucesso. Preserve decisao/evidencias para reconciliacao; a review
solicita correcao. Nao ha garantia de idempotencia completa desse fluxo.

Sem respostas processadas, confira poll, preservacao do cursor e roteamento
somente do chat autorizado. O pacote nao inicia esse loop nesta base. Com
varias perguntas use REF/correlation_id conforme [CONTROL_MESSAGES.md](CONTROL_MESSAGES.md).
O consumidor do relatorio formatado das 17:00 ainda nao esta integrado.

## Agente em cooldown ou fila parada

Confira rate_limited_agents no health e dependencias/estados reais. Cooldown
de agente difere de quota GitHub. mark_rate_limited persiste reset e permite
fallback FLEX READY para alternativa disponivel e compativel com a revisao.
IN_PROGRESS permanece no agente; ambos limitados significa aguardar.

Nao limpe limites para simular disponibilidade. Reset desconhecido/invalido
permanece conservador no health; corrija somente com evidencia.
mark_agent_available altera estado, nao sonda capacidade real.

READY sozinho nao basta: dependencias devem existir e estar DONE. Gates
BLOCKED/NEEDS_LUCAS exigem resolucao; outras elegiveis seguem. Prioridade nao
remove dependencia. Resposta a ultima decisao devolve tarefa a PLANNED para
readmissao por horario/cutoff, sem iniciar agente automaticamente.

check_idle considera atividade de READY elegiveis, cooldown e pausas; registra
uma decisao por episodio inexplicado, sem iniciar executor nem desfazer pausa.
unknown/stale de Jarvis/planner/scheduler exige verificar o produtor de heartbeat;
consultar health nao o renova. Veja [HEALTHCHECK.md](HEALTHCHECK.md) e
[SCHEDULER.md](SCHEDULER.md).

## Processo, banco, merge e testes

START/STOP/RESTART/STATUS/LOGS: [PROCESS_COMMANDS.md](PROCESS_COMMANDS.md).
Feche o launcher antigo com loop antes da adocao. identity_mismatch exige
inspecao de PID/comando; nao mate processo alheio. LOGS informa caminho,
sem imprimir conteudo. Revise logs localmente antes de compartilhar trechos.

Fila vazia apos trocar checkout/worktree: confira caminho do Store. O default
fica na raiz do checkout do pacote, derivado de __file__, independentemente do
diretorio de trabalho. Um caminho relativo passado explicitamente depende do
diretorio do processo. Compartilhe banco explicito. Nao apague banco/WAL/chaves
como recuperacao. Contagens None no health significam leitura nao confirmada.

Merge exige review vinculada a head/repo/PR/Task, checks requeridos completos
com sucesso, base correta e PR elegivel. Rebase pode invalidar evidencia antiga.
Nao fabrique REVIEW_PASSED para destravar. CLI 0 nao basta: merge_policy consulta
estado remoto. MERGE_COMPLETED nao muda sozinho Task para DONE nesta base.

Use python -m pytest no ambiente de [AGENTS.md](../../AGENTS.md). Testes Windows
usam processos descartaveis; Linux os pula. backdate legado FAIL403 difere de
pytest/Windows: registre cada check. Mocks nao validam credenciais ou E2E;
validacoes reais pertencem #45/#46.
