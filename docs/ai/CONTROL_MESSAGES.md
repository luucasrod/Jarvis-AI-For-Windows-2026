# Mensagens do canal de controle (#31)

`handle_control_message(text, *, store=None, plan_fn=None, send_fn=None)` em
`orchestrator/decisions.py` interpreta texto que **ja passou pelo filtro de
chat de controle da #19**. Nao e um endpoint publico/autenticador. O runtime
deve passar somente eventos desse canal; wiring do monolito continua na #30.

O retorno `ControlResult` informa `kind` (`plan`, `decision`, `priority`,
`clarification` ou `error`), mensagem, plano/identificadores quando aplicaveis
e `delivered` (confirmacao enviada pelo transporte da #19). Falha de envio
nao desfaz alteracoes locais ja confirmadas. O parser usa heuristica
deterministica; somente um objetivo reconhecido chama o LLM do planner.

## Formas aceitas

| Intencao | Exemplos |
| --- | --- |
| Objetivo novo | `Objetivo: criar testes do Argos`, `Quero que o Argos crie uma tela de status`, `Cria uma tela no Cashy` |
| Resposta a unica decisao pendente | `B`, `opcao B`, `Pode usar a opcao B`, `Sim`, `Nao` |
| Resposta citada | `REF: ab12cd34 B`, `Resposta <correlation_id> Mantenha o plano atual`, `Resposta <ID da tarefa> Sim` |
| Prioridade | `Prioriza <ID da tarefa>`, `Prioriza a tarefa <ID>`, `Prioriza "Titulo exato"` |

Opcoes reconhecem acentos e maiusculas/minusculas. Uma letra so e aceita se
consta das opcoes da pergunta salva. Texto livre exige referencia explicita.
`Objetivo:` e a forma recomendada para objetivos fora dos verbos reconhecidos
(criar, implementar, corrigir, adicionar e `quero que`). Negacoes ou frases
vagas como `Talvez depois` / `Quero a opcao B` pedem esclarecimento.

Com varias decisoes abertas, `B` sozinho nunca escolhe a primeira. Use a REF
da mensagem da #33 ou o correlation_id completo. Um ID de tarefa so basta
se identificar uma unica decisao aberta. Referencia ausente, ja respondida,
inexistente ou duplicada nao e convertida em objetivo novo. Colisoes de REF
tambem pedem esclarecimento; o correlation_id completo permite desambiguar.
IDs numericos de Issue GitHub nao sao inferidos: o modelo atual nao fornece
esse mapeamento. Use o ID local completo ou titulo unico para prioridade.

## Efeitos e integracao

- Objetivos chamam `planner.plan` real por padrao. `ControlResult.plan` retorna
  o resultado para #23/runtime publicar e persistir; este parser nao duplica
  essa ponte nem inicia tarefas. O gate humano e as dependencias do planner
  continuam intactos. Um plano retornado nao significa execucao iniciada.
- Respostas alteram a decisao exata, incluem o texto no contexto persistido da
  tarefa e emitem `DECISION_RECEIVED` com o correlation_id da decisao,
  task_id, agente preferido e `resume_requested`. Uma recusa e preservada
  integralmente: o executor deve respeitar a resposta, nao tratar toda resposta
  como aprovacao. A ultima resposta de uma tarefa `NEEDS_LUCAS` muda seu estado
  para `PLANNED`; scheduler/queue reavaliam horario, dependencias e elegibilidade.
  Outra decisao pendente preserva `NEEDS_LUCAS`. Outros estados ativos/bloqueados
  nao sao desbloqueados; tarefas encerradas ou ausentes nao consomem respostas.
  Nao ha chamada automatica a Paperclip nem troca do agente atribuido.
- Prioridade sobe para `urgent` sem mudar dependencias, estado ou agente.
  `get_priority_queue(store)` retorna tarefas elegiveis usando #28, ordenadas
  por urgent/high/medium/low, depois criacao e ID. Tarefas ativas/em review ou
  encerradas nao sao alteradas. Bloqueios continuam excluidos da fila elegivel.

Resposta, contexto da tarefa, readmissao e evento sao atomicos via
`Store.run_in_transaction`. Chamadas concorrentes por conexoes diferentes so
consomem uma decisao uma vez. A selecao e revalidada dentro da transacao;
se a pergunta mudar durante a selecao, o parser pede nova confirmacao da
referencia. A rede ocorre depois do commit, nunca dentro da transacao SQLite.
Eventos e dados persistidos sobrevivem a reinicio; retomada efetiva do agente
pertence ao consumidor de runtime desse evento/estado.

## Validacao

`tests/test_control_parser.py` usa transportes falsos e, para o objetivo,
a pipeline real de tres etapas do planner com geracao LLM injetada. Cobre
decisoes simultaneas, respostas repetidas, recusas, prioridade, rollback por
falha de evento e concorrencia entre conexoes. Nao envia Telegram real; o
teste de credenciais/canal real continua na #46.
