# Contexto local do Jarvis

Referencia factual da issue #10, baseada em `integration/orchestration` em
2026-09-12, no commit `d187af9`. Confirme o codigo da branch e o estado atual do GitHub antes de
assumir que um PR citado ja esta integrado ou aprovado.

## Duas camadas no mesmo repositorio

`main.py` executa o assistente de voz para Windows. `orchestrator/` contem
modulos para planejar, persistir e coordenar tarefas; sua existencia ainda nao
significa que o ciclo diario esteja conectado ao assistente em execucao.

O [README](../../README.md) apresenta o projeto original, mas alguns detalhes
ficaram antigos: o caminho atual de captura usa sounddevice, o chat usa Groq,
ha wake word e memoria em arquivo, e a saida tenta Piper antes dos fallbacks.
Para desenvolvimento, Python 3.12 e a referencia do workflow de testes.

## Voz existente

1. `take_command` captura audio mono com sounddevice e detecta fala/silencio.
   Tenta transcrever via Groq `whisper-large-v3-turbo`; se falhar, usa
   SpeechRecognition com reconhecimento Google em portugues.
2. O loop principal exige o wake word Jarvis, retira-o da frase e encaminha o
   comando a `_dispatch` / `process_command`. Comandos locais incluem apps,
   sites, volume, temporizadores e outras acoes do Windows.
3. `chat` usa Groq com streaming. `_choose_model` seleciona os identificadores
   `openai/gpt-oss-20b` ou `openai/gpt-oss-120b` segundo complexidade da frase.
   Esses sao valores do codigo, nao uma garantia de disponibilidade externa.
4. `ai` usa Gemini `gemini-2.5-flash` para respostas salvas. O planner tambem
   possui seu proprio adaptador Gemini; nao reutiliza o loop de audio.
5. `say` tenta Piper local, depois edge-tts, e por fim pyttsx3. O startup tenta
   preparar Piper/modelo em background. Nao e correto chamar toda a voz de
   offline: transcricao e LLM ainda dependem de servicos externos.

`jarvis_memory.json` guarda historico de conversa. `.jarvis.pid` identifica a
instancia; quando executado por pythonw, a saida e redirecionada a `jarvis.log`.
O monolito possui um watchdog e rotas internas de reinicio, mas isso nao substitui
um servico Windows gerenciado. Veja [AGENTS.md](../../AGENTS.md) para executar e
reiniciar pela consola sem afetar outros processos.

Ha integracoes domesticas legadas no monolito, incluindo WiZ, eWeLink e Argos.
Elas sao contexto da voz existente; este repositorio nao e o lugar para duplicar
a documentacao de Home Assistant, Docker ou WSL do SecondBrain.

## Paperclip existente

`paperclip_client.py` concentra chamadas HTTP e retornos estruturados. Ja oferece
snapshot, disponibilidade, busca de agente, pausa, retomada e criacao de tarefa.
O monolito consulta esse cliente para relatorios e comandos; os comandos de
escrita da interface de voz passam pela confirmacao existente em `main.py`.

O endpoint local padrao e `http://127.0.0.1:3100`. O comentario de implantacao
existente descreve modo local_trusted em loopback; nao presuma que uma instalacao
remota use o mesmo modo. O cliente legado le configuracao opcional do config.py
local. A extensao idempotente/status da issue #18 e trabalho separado, no PR #86
na data desta referencia; confira o merge antes de usa-la.

## Modulos e estado da orquestracao

O plano esta distribuido pelos EPICs
[#1](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues/1),
[#2](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues/2),
[#3](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues/3),
[#4](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues/4),
[#5](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues/5),
[#6](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues/6) e
[#7](https://github.com/luucasrod/Jarvis-AI-For-Windows-2026/issues/7)
e suas issues filhas. A politica de execucao e revisao esta no
[WORK_PROTOCOL](WORK_PROTOCOL.md); ele e a fonte operacional, nao uma lista de
features prontas. O checkpoint ativo e `integration/orchestration`; a baseline
`integration/wave-0` esta congelada, e `main` permanece protegida.

Nesta base ha implementacoes de:

| Area | Arquivos / issues |
| --- | --- |
| Configuracao, dados e persistencia | `config.py`, `models.py`, `persistence.py` (#9, #12, #13, #16) |
| Eventos e auditoria | `events.py`, `audit.py` (#14, #20) |
| Contexto de projeto e fronteira de dados externos | `project_resolver.py`, `security.py` (#15, #21) |
| Planejamento e politica de agentes | `planner.py`, `agent_policy.py`, `review_pipeline.py` (#22, #24, #29) |
| Telegram, decisoes e historico | `telegram_bot.py`, `decisions.py`, `history.py` (#19, #33, #34) |

Os caminhos da tabela sao relativos a `orchestrator/`. Alguns desses modulos
tem correcoes em revisao; implementacao integrada nao implica review PASS.
O planner entende, planeja, critica e decompoe um objetivo em tarefas; ele nao
executa sozinho cada tarefa nem instala o ciclo diario no monolito.

Na referencia desta documentacao, GitHub (#17/PR #90), extensao Paperclip
(#18/PR #86), scheduler (#25/PR #82) e continuidade da fila (#28/PR #84) foram
publicados em branches de issue para revisao. Seus contratos devem ser lidos
no respectivo PR ate a integracao. Nao os trate como APIs presentes numa base
que ainda contenha os stubs.

Ainda sao roadmap ate as respectivas entregas: distribuicao/execucao (#23),
rate limit e idle (#26/#27), wiring diario (#30), parsing de controle (#31),
relatorio de 17:00 (#32), fachada de voz (#35), healthcheck (#36),
merge/deploy/offline/idempotencia/servico (#37-#42) e validacoes finais (#44-#46).
Nao ha, nesta base, um entrypoint executavel que conecte todo esse ciclo.

## Dados e configuracao

`Task` e uma dataclass. Os valores de `TaskState` implementados sao:
`INBOX`, `PLANNED`, `NEXT_CYCLE`, `READY`, `IN_PROGRESS`, `IN_REVIEW`, `BLOCKED`,
`NEEDS_LUCAS`, `BUG_FOUND`, `DONE`, `FAILED`. O enum nao implementa transicoes por
si so; planner, fila, revisao e runtime sao responsaveis por cada transicao.

`AgentClass` distingue `CLAUDE`, `CODEX`, `FLEX`. `ExecutionMode` distingue
`PARALLEL`, `SOLO`; SOLO e modo de execucao, nao um agente. `AgentName` possui
`Claude`, `Codex`, `either`, `none`; preferencias abstratas precisam ser resolvidas
antes de uma execucao/review concreta.

`Store` usa SQLite no arquivo local `orchestrator_state.db`, por padrao, com WAL
e protecao por lock para uso entre threads. Persiste tarefas, decisoes, rate
limits, sincronizacao e chaves de idempotencia. O log de eventos usa o mesmo
banco. A aplicacao consumidora ainda precisa respeitar as fronteiras atomicas
das operacoes; um par de chamadas separadas nao se torna uma transacao por ter lock.

A voz importa `apikey` e `groq_apikey` do `config.py` da raiz, nao versionado.
`orchestrator/config.py` e outro arquivo, versionado: carrega `.env` opcional,
preserva precedencia das variaveis de ambiente e fornece os parametros descritos
em `.env.example`. Nao suponha que preencher `.env` substitua automaticamente os
imports legados da voz. Validacao de configuracao avisa sobre integracoes ausentes.

O resolver usa `SECONDBRAIN_INDEX_PATH`, cujo default e
`A:\SecondBrain\project_context_index.json`. O historico de decisoes de
infraestrutura esta em `A:\SecondBrain\01-Projects\Casa-Inteligente\Contexto.md`.
Referencie essa nota quando necessario; nao copie seu conteudo para ca. Esses
caminhos sao locais e podem precisar de configuracao em outra maquina.

## Validacao

Os testes estao em `tests/`, descobertos por `pytest.ini`. O CI
`.github/workflows/tests.yml` usa Python 3.12 e dependencias minimas de teste;
`requirements.txt` inclui tambem as dependencias de voz para instalacao local.
Rode `python -m pytest` no ambiente preparado. Nao importe/inicie `main.py` para
testar um modulo puro: ele depende de Windows, configuracao local e SDKs de voz.

Mocks de transporte/relogio demonstram contratos locais, nao entrega real de
Telegram ou funcionamento completo do ciclo diario. Testes reais e E2E possuem
issues proprias. Verifique GitHub, testes e review cruzada antes de declarar
integracao, e use o WORK_PROTOCOL para a proxima tarefa.
