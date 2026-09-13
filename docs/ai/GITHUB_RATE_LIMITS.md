# Limites do GitHub e retomada no startup (#40)

O cliente da #17 passa `--include` para receber os headers de resposta da CLI.
Com `--paginate --slurp`, a CLI coloca um bloco HTTP antes de cada pagina,
dentro do array externo; o parser remove esses blocos antes de decodificar
JSON. Esse formato foi verificado numa leitura real de duas paginas. Strings
de Issues com texto parecido com headers continuam dados, sem serem removidas.
Uma falha numa pagina posterior invalida toda a listagem para deduplicacao.

Em HTTP 429 ou 403 com indicacao de rate limit, o cliente respeita:

- `Retry-After` em segundos ou data HTTP valida;
- `X-RateLimit-Reset` em epoch UTC quando `X-RateLimit-Remaining=0`;
- se ambos impuserem restricoes, o maior prazo;
- sem prazo utilizavel, pelo menos 60s para rate limit identificado, alem do
  backoff exponencial da operacao quando este exigir mais tempo.

O teto do backoff generico nunca encurta uma espera exigida pelo servidor.
403 de autenticacao/permissao sem indicacao de quota nao e confundido com
rate limit global. Nao ha sleep dentro do cliente nem alteracao de autenticacao.
O contrato segue as
[recomendacoes oficiais do GitHub](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#handle-rate-limit-errors-appropriately).

O prazo fica em `sync_state` para github.com, compartilhado pelas operacoes e
instancias de cliente que usam o mesmo banco do runtime. Uma observacao menor
nao encurta uma maior. Enquanto vigente, listagens/criacoes novas nao fazem
chamadas ao GitHub. Mesmo apos reinicio, operacoes respeitam o prazo persistido
e `next_attempt`; resultados ja concluidos continuam disponiveis em cache.
O escopo e conservador por host/banco, nao por token: trocar a credencial nao
apaga uma restricao observada. Nenhum header bruto, token ou corpo de erro e
persistido como diagnostico.

## Startup

`client.reprocess_pending_ops(limit=20)` reutiliza `retry_pending`: processa
uma rodada de operacoes vencidas, sem roubar leases ainda validos, zerar prazos
ou repetir POST incerto. Tambem ha uma entrada de modulo:

```python
from orchestrator.github_client import reprocess_pending_ops

results = reprocess_pending_ops(runtime_store)
```

O wiring de startup da #30 deve chamar essa entrada uma vez apos abrir o Store;
depois, o tick periodico usa o retry normal para o restante da fila e operacoes
que ainda nao venceram. `limit` e tamanho de lote, nao descarte/cap da fila.
Sem Store explicito, a funcao abre e fecha uma conexao ao banco configurado
pelo Store. Importar o modulo/instanciar o cliente nao dispara rede por si so.
Este PR nao altera main.py, reservado a issue SOLO de wiring.

Operacoes `done`/`failed` nao sao reenviadas. Uma criacao incerta se reconcilia
pelo marcador remoto da #17; lease expirado pode ser retomado, lease vigente
aguarda. HTTP 429/403 rejeitado confirma que aquele POST nao criou a Issue;
quando permitido, o retry ainda faz a deduplicacao normal antes de um novo POST.

Testes exercitam respostas reais em formato de headers (transporte fake),
paginas parciais, espera alem do teto generico, duas instancias, reabertura de
SQLite e timeout depois de criacao remota. O smoke real e somente leitura;
nenhuma quota foi esgotada deliberadamente nem Issue criada pela #40.
