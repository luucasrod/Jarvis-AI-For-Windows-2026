# Contexto local do Jarvis

Referencia da issue #43: `integration/orchestration`, commit `e23a74e`, em
2026-09-13. Confirme codigo e GitHub antes de assumir que esta fotografia
descreve uma entrega posterior.

## Estado da integracao

`main.py` executa a voz no Windows. O pacote `orchestrator/` fornece planejamento,
persistencia, integracoes e politicas testadas separadamente. Nesta referencia,
`main.py` nao importa o pacote e nao existe `orchestrator/orchestrator.py` no
checkpoint. Iniciar o Jarvis pelos atalhos nao inicia um ciclo diario completo.

O [WORK_PROTOCOL](WORK_PROTOCOL.md) define revisao cruzada e integracao.
`integration/wave-0` esta congelada; `integration/orchestration` e o checkpoint
ativo. `main` recebe somente checkpoints aprovados. O PR historico #47 fica
aberto e intocado. Merge em integracao nao equivale a review PASS.

| Area | Estado nesta referencia |
| --- | --- |
| Ciclo diario #30 | PR #130 aberto; review #131 solicita correcoes de admissao, projetos, dispatch, atribuicao e janela do relatorio. Nao integrado. |
| Ponte de voz #11/#35 | Conexao do pacote ao monolito e respostas reais da fachada pendentes. |
| Relatorio #32 | Transporte e evento de horario existem; formatacao e consumo automatico pendentes. |
| Deploy #38 | Descoberta, smoke e criacao automatica de bug pendentes. |
| Idempotencia #41 | PR #132 integrado; review #133 encontrou duplicacao Telegram em crash, timeout e concorrencia. |
| Configuracao/E2E #44-#46 | Auditoria final e validacoes completas/controladas/reais pendentes. |

Consulte as [issues do projeto](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues)
para mudancas posteriores. A tabela nao e atualizada automaticamente pelo runtime.

## Voz e processo Windows

`take_command` captura audio mono com sounddevice e tenta transcrever via Groq
`whisper-large-v3-turbo`, com fallback SpeechRecognition/Google em portugues.
O loop exige o wake word Jarvis e encaminha comandos a `_dispatch` /
`process_command`. `chat` usa Groq com streaming e seleciona
`openai/gpt-oss-20b` ou `openai/gpt-oss-120b` conforme complexidade. `ai` usa
Gemini para respostas salvas. Esses identificadores descrevem o codigo, sem
garantir disponibilidade externa dos modelos.

`say` tenta Piper, edge-tts e pyttsx3. O startup prepara Piper/modelo em
background. Transcricao e LLM podem exigir rede; a voz inteira nao e offline.
`jarvis_memory.json` guarda memoria local. `.jarvis.pid`, `jarvis.log` e o
watchdog pertencem ao monolito. Integracoes WiZ, eWeLink, Argos e comandos
Paperclip legados continuam nele; escritas pela voz conservam sua confirmacao.

START/STOP/RESTART/STATUS/LOGS da #42 estao documentados em
[PROCESS_COMMANDS.md](PROCESS_COMMANDS.md), com atalhos, identificacao de
processos e compatibilidade com o launcher antigo. A estrategia usa PowerShell
e `.bat`; nao instala servico nem inicio automatico no logon. Processo vivo
nao confirma saude de audio, LLM ou scheduler. A #35 trata a fachada de voz,
nao substitui esses comandos de processo.

## Modulos e contratos

Os caminhos desta tabela sao relativos a `orchestrator/`.

| Modulo | Responsabilidade implementada |
| --- | --- |
| `project_resolver.py` | Resolve projeto canonico e metadados do indice local. Ambiguidade nao autoriza escolher destino arbitrario. |
| `planner.py` | Planeja, critica e decompoe objetivo; geracao LLM injetavel, gates humanos, referencias invalidas/ciclos bloqueados e deduplicacao. Retorna PlanResult, nao executa agentes. |
| `security.py` | Delimita conteudo externo como dados e detecta sinais de injecao. |
| `agent_policy.py` | Classifica tarefa, preferencia/fallback/revisor e hotspots SOLO. Preferencia nao e ID Paperclip. |
| `task_queue.py` | Calcula elegibilidade por dependencias DONE e materializa plano em Issues ou fila local declarada. |
| `scheduler.py` | Admite tarefas e registra ciclo/cutoff/relatorio com horario e guards persistidos. Nao inicia thread. |
| `agent_availability.py` | Persiste cooldown e redireciona FLEX READY quando existe alternativa permitida; preserva trabalho em andamento. |
| `review_pipeline.py` | Exige revisor distinto, registra resultado/escalacao. O consumidor precisa aplicar recommended_state. |
| `merge_policy.py` | Exige review do mesmo head/repo/PR/tarefa, checks, base e estado; merge condicionado ao head, com reconciliacao atomica de evento/audit. |
| `github_client.py` | CLI gh, criacao idempotente, fila persistente, reconciliacao e rate limit. |
| `paperclip_ops.py` | Criacao/status pela API existente e PaperclipSession com backoff/deteccao de restart. |
| `telegram_bot.py` | Canais separados, filtro de updates de controle e evidencia de entrega quando recebe Store. |
| `decisions.py` | REF/pergunta, persistencia de decisoes, parsing de controle e prioridade. |
| `history.py` | Resumo de eventos desde um instante; nao envia relatorio. |
| `healthcheck.py` | Evidencia de saude e diagnostico de ociosidade; nao inicia recuperacao automatica. |
| `models.py`, `persistence.py`, `events.py`, `audit.py` | Modelos, SQLite, eventos e auditoria com redacao de segredos em extras. |

O fluxo a compor e: resolver projeto -> planejar -> persistir/admitir ->
selecionar elegiveis -> materializar -> atribuir agente remoto -> observar
execucao -> revisao cruzada -> merge -> historico/relatorio. Importar os modulos
nao executa essa cadeia. Criacao Paperclip nao comprova atribuicao ou progresso:
o consumidor precisa identificar o agente concreto e observar o estado remoto.

`materialize_plan` respeita a fonte de tarefas do projeto. A fila local precisa
resolver dentro da raiz canonica e usa marcador por correlation_id. Dependencias
ausentes, ciclos e gates adiam materializacao. Atualizacao de relacionamentos
GitHub preserva corpo existente. `list_incomplete_relationships(store, repo=None)`
lista reparos pendentes com delta; a consulta nao inicia reparo automatico.

## Estados, persistencia e recuperacao

`TaskState`: INBOX, PLANNED, NEXT_CYCLE, READY, IN_PROGRESS, IN_REVIEW, BLOCKED,
NEEDS_LUCAS, BUG_FOUND, DONE, FAILED. O enum nao executa transicoes.
`AgentClass`: CLAUDE/CODEX/FLEX; `ExecutionMode`: PARALLEL/SOLO;
`AgentName`: Claude/Codex/either/none.

`get_promotable_tasks` considera PLANNED/NEXT_CYCLE/READY, exigindo dependencias
presentes e DONE, sem cap de fila. Nao altera entrada nem libera gates humanos.
`get_priority_queue` ordena elegiveis por urgent/high/medium/low, criacao e ID.
O dispatcher ainda precisa aplicar horario, projeto e disponibilidade.

`Store` usa SQLite em `orchestrator_state.db` na raiz do checkout do pacote
por padrao (caminho absoluto derivado de __file__), com WAL e lock local.
Use o mesmo banco explicitamente nos componentes para compartilhar tarefas,
decisoes, eventos, limites, guards e operacoes. Duas chamadas separadas nao
formam transacao: `run_in_transaction` / `run_sync_once` usam BEGIN IMMEDIATE
entre conexoes. Eventos/audit podem participar da mesma transacao; callbacks
usam a conexao recebida, sem rede ou commits internos.

GitHub reconcilia criacao incerta por marcador; Paperclip usa chave de operacao
e resultado duravel. Nao apague claims/banco para forcar retry. Merge confirma
estado remoto depois da CLI e registra conclusao atomicamente; atualizar Task
continua responsabilidade do consumidor. Telegram tem lacuna conhecida na
#133: chave posterior ao envio nao protege crash antes do commit, timeout de
resposta ou concorrencia. Nao ha garantia de ausencia de duplicatas nesse envio.

## Horarios e interacao

Defaults: Europe/Lisbon, inicio 08:00, cutoff 14:00, relatorio 17:00. Janela de
admissao: inicio inclusivo/cutoff exclusivo. Cutoff nao interrompe IN_PROGRESS;
tarefas novas fora da janela vao para NEXT_CYCLE. Guards por data protegem
efeitos locais apos restart, DST e recuo do relogio. REPORT_TIME_REACHED nao
significa mensagem enviada. Veja [SCHEDULER.md](SCHEDULER.md).

Telegram separa TELEGRAM_CONTROL_CHAT_ID de TELEGRAM_REPORT_CHAT_ID. Poll e uma
chamada, nao loop persistente; o runtime precisa preservar cursor e rotear
somente texto autorizado. [CONTROL_MESSAGES.md](CONTROL_MESSAGES.md) descreve
objetivos, prioridade e respostas por REF. A ultima decisao respondida pode
devolver NEEDS_LUCAS a PLANNED para readmissao, sem retomar agente automaticamente.

`check_idle` considera READY elegiveis, atividade, cooldown e pausas explicadas.
Emite DECISION_REQUIRED uma vez por episodio inexplicado; nao remove pausas
deliberadas nem envia sozinho a pergunta. [HEALTHCHECK.md](HEALTHCHECK.md)
explica ok/offline/unconfigured/unknown/stale. Heartbeats devem vir dos
componentes vivos; consultar saude nao os renova.

Detalhes: [GitHub](GITHUB_CLIENT.md), [quota/retry](GITHUB_RATE_LIMITS.md),
[Paperclip](PAPERCLIP_OPS.md), [restart/backoff](PAPERCLIP_RESILIENCE.md).
`reprocess_pending_ops` exige chamada do runtime; PaperclipSession exige reuso.
Nenhum deles instala worker em background.

## Configuracao e validacao

A voz importa `apikey` e `groq_apikey` do `config.py` local da raiz, nao
versionado. `orchestrator/config.py` e outro arquivo: carrega `.env` opcional,
preservando precedencia de variaveis de ambiente. [.env.example](../../.env.example)
lista configuracao. Nao sobrescreva arquivos locais existentes. O cliente le
PAPERCLIP_API_TOKEN quando necessario; local_trusted em loopback nao garante
acesso sem autenticacao a qualquer URL remota.

`validate_config` avisa sobre Telegram ausente e retry/timeout invalidos;
nao valida credenciais nem cobre toda configuracao final. Scheduler valida
fuso/horarios; auditoria completa e #44. SECONDBRAIN_INDEX_PATH aponta por
default para `A:\SecondBrain\project_context_index.json`; configure o caminho
real da maquina. A nota local de infraestrutura continua em
`A:\SecondBrain\01-Projects\Casa-Inteligente\Contexto.md`, sem copiar seu conteudo
para este repo. Nao versione tokens, memoria, bancos ou logs.

Use Python 3.12 como referencia conforme [AGENTS.md](../../AGENTS.md).
`python -m pytest` valida a suite; CI Linux cobre o pacote e Windows executa
testes de processo em fixtures descartaveis. Mocks nao provam credenciais,
entrega real ou E2E. Nao importe main.py para testar funcoes puras. Reporte
checks separadamente: backdate legado pode falhar com 403 enquanto pytest e
Windows passam. Veja [TROUBLESHOOTING.md](TROUBLESHOOTING.md) para diagnostico.
