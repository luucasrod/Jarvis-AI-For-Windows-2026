# Jarvis: entrada para agentes

Este repositorio contem o assistente de voz do Windows e a construcao de uma
camada de orquestracao de tarefas. Preserve a voz existente enquanto implementa
as issues do orquestrador em modulos separados.

Leia primeiro [docs/ai/CONTEXT.md](docs/ai/CONTEXT.md), o
[README](README.md) e [docs/ai/WORK_PROTOCOL.md](docs/ai/WORK_PROTOCOL.md).
O README descreve a origem do projeto; o contexto local explica diferencas do
codigo atual, inclusive Groq, Piper e o estado ainda parcial da orquestracao.

## Coordenacao

- Confirme o protocolo na branch remota de integracao antes de escolher trabalho.
  O checkpoint ativo documentado e `integration/orchestration`;
  `integration/wave-0` esta congelada. Nao abra PR novo contra a baseline congelada.
- Use uma branch e um worktree por issue. Leia issue, dependencias, comentarios e
  PRs existentes antes de reservar trabalho; registre a reserva no GitHub.
- Siga a prioridade P0/P1/P2/P3 do WORK_PROTOCOL. Reviews sao tarefas reais:
  use a Review Task existente, sem duplicar. Quem implementa nao faz a propria
  review; Codex e Claude revisam o trabalho um do outro.
- `main` recebe apenas checkpoints aprovados. Merge em integracao nao equivale
  a revisao aprovada nem a funcionalidade ligada ao runtime de voz.
- O PR historico #47 deve permanecer aberto e intocado, conforme decisao do
  usuario. Nao o feche, edite ou use para promover trabalho novo.
- Instrucoes explicitas da conversa prevalecem sobre este guia. Coordenacao
  entre agentes ocorre por issues, comentarios, labels e PRs do GitHub.

## Limites de edicao

- `main.py` e o monolito de voz. So o edite nas issues SOLO especificamente
  destinadas a ele (#11, #30, #35), apos verificar exclusividade. Outra issue
  precisar desse arquivo e motivo para reler seu escopo.
- Implemente a issue no modulo indicado em `orchestrator/`. Se houver stub,
  substitua-o; nao crie uma segunda implementacao com outro nome.
- Reutilize `paperclip_client.py` para Paperclip. Nao edite `node_modules`,
  reconstrua Paperclip ou amplie a superficie de escrita fora da issue.
- Extensoes de persistencia/eventos devem ser aditivas e testadas. Preserve
  contratos existentes e documente mudancas semanticas necessarias no PR.
- Nao versione `config.py` da raiz, `.env`, `.venv`, tokens, memoria pessoal, logs ou
  bancos locais. Nunca imprima credenciais durante diagnostico. O padrao
  `/config.py` no gitignore e intencional: `orchestrator/config.py` e versionado.
- Nao remova arquivos locais desconhecidos ou alteracoes do outro agente.

## Comandos locais (PowerShell)

Use Python 3.12 como referencia, a versao exercitada pelo CI. A voz depende de
Windows, microfone e saida de audio; testes do orquestrador nao precisam iniciar
o monolito. Crie `.venv` somente se ainda nao existir; reutilize o ambiente
existente quando ele ja estiver preparado.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
```

Antes de iniciar a voz, configure o `config.py` local com `apikey` e
`groq_apikey`, conforme os imports de `main.py`. Para a camada de orquestracao,
copie `.env.example` para `.env` somente se esse arquivo ainda nao existir e
preencha as integracoes usadas. Nao sobrescreva configuracao local existente.

```powershell
.\.venv\Scripts\python.exe main.py
```

Para reiniciar uma instancia iniciada nessa consola, encerre-a com Ctrl+C e
execute o mesmo comando novamente. Nao mate todos os processos Python: outros
agentes e ferramentas podem estar usando-os. Para uma instancia oculta, identifique
primeiro o processo correto usando `.jarvis.pid` e sua linha de comando.

Nao ha script de servico start/stop/restart versionado nesta base; a issue #42
trata desse empacotamento. Atalhos `.bat` existentes apenas na maquina nao sao
parte da instalacao reproduzivel do repositorio.

## Validacao e entrega

Rode testes apropriados e a suite exigida pela issue. Testes de integracao usam
transporte/relogio injetados; nao dispare voz, automacoes domesticas, tarefas de
agentes ou mensagens reais para testar um wrapper sem necessidade autorizada.
Documentacao exige verificacao factual e de links, sem testes artificiais.

Registre commit, evidencia local e estado real de cada check do CI. Nao chame
o CI de inteiramente verde quando um workflow falhou. Crie a Review Task cruzada
sem duplicatas e siga os criterios de integracao do WORK_PROTOCOL. Nao declare
uma funcionalidade pronta somente porque o arquivo existe ou os testes passaram.
