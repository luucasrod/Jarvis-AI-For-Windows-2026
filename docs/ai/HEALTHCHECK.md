# Healthcheck agregado (#36)

`get_health_status()` em `orchestrator/healthcheck.py` devolve `HealthReport`.
Consulte `format_health_for_voice(report)` ou `format_health_for_telegram(report)`
para texto legivel. Nao ha envio automatico, recuperacao, restart ou loop de
monitoramento; #27 e #30 consomem esses contratos.

O relatorio distingue `ok`, `offline` (sonda/tentativa indisponivel),
`unconfigured`, `unknown` (sem evidencia valida) e `stale` (atividade expirada).
Configuracao presente ou arquivo importavel nao bastam para declarar saude.

| Componente | Evidencia usada |
| --- | --- |
| Paperclip | `is_available` usa URL/timeout da #16 e valida JSON `status=ok` de `/api/health` |
| GitHub | `gh api --hostname github.com /rate_limit`, timeout 10s, JSON valido e quota core disponivel |
| Telegram | Configuracao dos dois canais e ultima tentativa de envio confirmada em cada um |
| Jarvis, planner, scheduler | Heartbeat persistido recente de cada componente |

As sondas Paperclip/GitHub sao somente leitura e podem ser injetadas para
testes ou monitoramento com cooldown gerenciado pelo runtime. Cada chamada
padrao faz uma sonda de cada; nao cria Issues, tarefas ou mensagens. Excecoes
e respostas externas nao aparecem cruas no relatorio. Telegram nao e sondado
com uma mensagem de teste. Confirmacao recente nao garante entrega futura.

## Observacoes do runtime

O runtime chama `record_heartbeat(store, component)` periodicamente enquanto
`jarvis`, `planner` ou `scheduler` estiverem ativos, inclusive ociosos. O
healthcheck nunca renova seu proprio heartbeat: consultar saude nao prova que
esses loops estejam vivos. O prazo default e 120s, configuravel no argumento
`heartbeat_max_age_seconds`. Timestamp futuro, invalido ou ausente e desconhecido.
Este PR nao inicia nem conecta loops em main.py; ate #30/#27 produzirem os
sinais, esses componentes aparecerao como nao confirmados.

`last_cycle_at` vem dos guards `scheduler:<data>:cycle_start` da #25. E o ultimo
ciclo observado, nao prova de processo ativo nem de tarefas executadas: um
ciclo recuperado depois do cutoff pode apenas registrar o guard. Horario do
ciclo e heartbeat sao mostrados separadamente para nao esconder essa diferenca.

Para observar envios reais, passe `store=runtime_store` a
`send_control_message` / `send_report_message`. As assinaturas antigas continuam
validas. A extensao grava status e timestamps em sync_state, com identidade
hash do token/destino; mudar credenciais/chat nao herda saude anterior. Nao
persiste texto, chat_id, token ou erro bruto. Envio so e confirmado quando o
JSON retorna `ok=true` e `result` como objeto, conforme o
[contrato da Bot API](https://core.telegram.org/bots/api#making-requests).
HTTP 2xx isolado deixou de ser considerado confirmacao de envio.

`record_telegram_delivery(store, config, channel, ok)` tambem permite adaptar
um transporte externo que ja conhece o resultado real. Registre todas as
tentativas, inclusive falhas: uma falha recente prevalece sobre sucesso antigo.
Sinais antigos nao sobrescrevem observacoes mais novas. O prazo default de
envio observado e 48h (`telegram_max_age_seconds`), apropriado a um canal de
relatorio diario. Sem instrumentacao/envio em algum canal: `unknown`.
Falha ao persistir telemetria nao transforma um envio confirmado em falha de
transporte, evitando que o chamador repita uma mensagem ja entregue.

## Filas e limites

Uma fotografia curta de SQLite conta operacoes GitHub ainda pendentes/em voo
e claims Paperclip sem resultado confirmado em `pending_actions`. Operacoes
GitHub `failed` ficam em `failed_actions`; `done` nao conta. Tabelas de operacao
ainda nao criadas significam nenhuma operacao registrada. Se o banco nao
puder ser lido, contagens ficam `None`/nao confirmadas, nunca zero inventado.

`rate_limited_agents` consulta rate_limits da #13. Reset exatamente agora ja
esta expirado. Reset ausente ou invalido permanece conservadoramente ativo
ate o registro ser corrigido/limpo; nenhuma operacao de diagnostico libera
agentes. Razoes brutas do limite nao sao exibidas.

`report.ok` exige seis componentes confirmados, banco legivel, nenhuma acao
pendente/falha definitiva e nenhum agente em cooldown. As observacoes sobrevivem
ao reinicio do leitor, mas expiram; nao sao prova de disponibilidade permanente.
Nenhuma sonda de rede roda dentro da transacao SQLite.
