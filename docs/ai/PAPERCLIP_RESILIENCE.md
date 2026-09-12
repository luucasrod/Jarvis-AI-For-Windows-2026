# Paperclip: restart e backoff (#39)

`PaperclipSession` em `orchestrator/paperclip_ops.py` adiciona uma politica de
resiliencia sobre as funcoes existentes da #18. O runtime deve manter **uma
sessao por servidor** e reutiliza-la para criacao idempotente, leitura de status
e deteccao de restart. Criar uma sessao em cada chamada elimina o backoff.
O wiring no monolito continua nas issues especificas de runtime.

```python
from orchestrator.paperclip_ops import PaperclipSession

paperclip = PaperclipSession()
restarted = paperclip.detect_restart()
health = paperclip.last_runtime_status
result = paperclip.get_task_status(company_id, task_id)
# paperclip.create_task_idempotent(company_id, title, body, correlation_id,
#                                  store=runtime_store)
```

`detect_restart()` compara `serverInfo.processStartedAt` de `/api/health`.
Esse campo foi confirmado por leitura do servidor local em 2026-09-12.
A primeira leitura valida estabelece a referencia e retorna False; uma mudanca
retorna True uma vez. Instantes equivalentes com fusos diferentes sao iguais.
Versao de software, indisponibilidade ou JSON invalido nao provam restart.
Uma falha preserva a ultima identidade valida. Servidor antigo sem esse campo
retorna `runtime_identity_unavailable`, sem inventar deteccao por versao/PID.
`last_runtime_status` distingue essa falha de um processo saudavel sem mudanca.

Todas as operacoes da sessao compartilham cooldown exponencial. A primeira
falha usa `RETRY_INTERVAL_SECONDS` (#16, default 30 segundos), dobra a cada
nova tentativa falha e limita-se a `max_backoff_seconds` (default 3600).
O limite deve ser pelo menos o intervalo inicial. Usa relogio monotonic;
o cooldown comeca ao terminar a chamada, inclusive se houve timeout.
Uma operacao bem-sucedida zera a sequencia. Respostas JSON com formato errado
tambem entram em cooldown. Falhas locais/de validacao retornadas pela #18 sao
tratadas conservadoramente com o mesmo intervalo, nao geram loops imediatos.

Antes do prazo, a sessao retorna `available=False`, `reason=backoff` e
`retry_after_seconds`, sem rede nem sleep. `uncertain=True` nesse retorno e
conservador: uma chamada anterior pode ter criado a tarefa antes de perder a
resposta. Ao vencer o prazo, o chamador decide quando tentar novamente; a #18
continua reconciliando pela chave persistida e nunca repete um POST incerto.
Durante cooldown, ate resultados locais em cache aguardam a proxima chamada
permitida. Metodos da mesma sessao sao serializados, incluindo a tentativa de
recuperacao, para evitar uma rajada concorrente depois de uma falha.

A sessao guarda cooldown e identidade apenas em memoria. Reiniciar o Jarvis
estabelece nova referencia; nao permite inferir reinicios ocorridos enquanto
ele estava parado. A idempotencia da #18 permanece persistida em SQLite e nao
e apagada por restart do Paperclip. As funcoes legadas sem sessao continuam
disponiveis para consumidores que ja controlam suas proprias tentativas.
Nao ha restart automatico, thread de retry, escrita de runtime-info local ou
alteracao de Paperclip/node_modules.
