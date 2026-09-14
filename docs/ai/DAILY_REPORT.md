# Relatorio diario (#32)

`orchestrator.reporting.process_report_tick(store, config=..., clock=...)`
compoe `Scheduler.on_report_time`, `run_report`, `format_daily_report` e o
transporte existente `send_report_message`. O chamador deve executar um tick
periodico com o mesmo Store. O horario vem de `REPORT_TIME` (17:00 por padrao)
e `TIMEZONE`; antes dele nao cria um novo evento. Eventos de horario ja
persistidos podem ser consumidos mesmo antes do horario do dia seguinte.

O modulo nao inicia thread, nao altera `main.py` e nao instala um servico.
A entrega automatica no processo depende de o runtime chamar esse tick.
Importar o modulo sozinho nao envia mensagens. A integracao com voz permanece
nas issues SOLO do monolito.

## Conteudo e canal

O relatorio usa as oito secoes da #32, contagens da #30 e titulos das tarefas
atuais no lugar dos IDs quando encontrados. Um periodo sem atividade nem
pendencias gera uma frase curta. Texto simples, sem JSON nem parse_mode, e
dividido em paginas de ate 4096 unidades UTF-16, preservando todo o conteudo e
preferindo quebras de linha. O destino e `TELEGRAM_REPORT_CHAT_ID`.

## Confirmacao e recuperacao

A tabela aditiva `daily_reports` guarda o ID do evento, instante de coleta,
paginas congeladas, proxima pagina, estado e hash da identidade bot/canal.
Nao guarda o token. Cada chamada processa o evento pendente mais antigo;
ticks seguintes drenam os demais eventos. A coleta corresponde ao instante
em que foi feita, nao reconstitui historicamente o estado as 17:00 de um dia
em que o processo estava desligado.

Antes de cada POST, uma transacao muda `pending` para `uncertain`. Outra
conexao ou um processo reiniciado nao repete essa pagina. Quando o transporte
confirma o envio, a proxima pagina e persistida. A ultima confirmacao grava
`delivered` e avanca `orchestrator:last_report_at` na mesma transacao, usando
o instante da coleta, nunca um horario posterior de entrega. Um cursor mais
recente nao e regredido. Esse e o contrato de `mark_report_delivered` da #30,
aplicado atomicamente com a confirmacao local.

Estados retornados em `ReportDelivery`:

- `delivered`: todas as paginas confirmadas; ticks posteriores nao repetem.
- `pending`: rejeicao explicita sem entrega (token/chat invalido); pode tentar
  de novo, preservando paginas ja confirmadas e o conteudo original.
- `unconfigured`: falta token ou canal; nenhuma tentativa feita.
- `uncertain`: timeout, erro de rede, resposta ambigua, envio ainda em curso
  ou crash entre a reserva e a confirmacao. Nao ha reenvio automatico.
- `destination_changed`: bot/canal mudou apos entrega parcial ou incerta;
  nao divide o mesmo relatorio entre destinos. Antes da primeira pagina,
  apos rejeicao explicita, permite corrigir a configuracao.

`None` significa que este tick nao adquiriu trabalho pendente. Uma entrega
incerta bloqueia os relatorios posteriores, preservando a ordem do cursor;
nao bloqueia a execucao de tarefas. O operador deve verificar o canal e a
tentativa em curso antes de reconciliar manualmente o registro no banco.
Nao apague registros nem redefina `uncertain` para `pending` sem confirmar
que o POST nao entregou: Telegram nao fornece chave idempotente neste wrapper.
Nao ha garantia de entrega exatamente uma vez nem recuperacao automatica de
resultado remoto desconhecido. Uma ferramenta de reconciliacao fica fora
desta issue de formatacao e entrega.

## Validacao

`python -m pytest tests/test_reporting.py` cobre secoes, dia vazio/parcial,
Unicode e limite, horario, transporte real com HTTP substituido, canal report,
reabertura do SQLite, retomada de paginas, configuracao, concorrencia entre
conexoes e crash apos envio antes da confirmacao. Nao envia Telegram real.
