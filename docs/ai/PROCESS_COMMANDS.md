# Comandos de processo do Jarvis (#42)

Estrategia: manter os atalhos `.bat` aprimorados sobre um unico script nativo
PowerShell. A voz precisa da sessao interativa do Windows para microfone e audio;
um servico acrescentaria instalacao e contexto de usuario a administrar. Task
Scheduler/Startup so seriam necessarios para inicio automatico no logon, que nao
foi escolhido aqui. Nenhuma tarefa agendada, servico ou entrada de registro e
instalada. O inicio continua sendo uma acao do usuario, em uma unica chamada.

O processo inicia oculto e continua apos fechar o terminal. `main.py` conserva
seu PID, redirecionamento para `jarvis.log` e watchdog; nao foi modificado.
Nao existe um segundo loop de reinicio que ressuscite Jarvis apos STOP.

## Uso

Prepare `.venv` e a configuracao local conforme [AGENTS.md](../../AGENTS.md).
Execute os atalhos na raiz pelo terminal ou use PowerShell diretamente:

| Acao | Atalho | Comportamento |
| --- | --- | --- |
| START | `Iniciar_Jarvis.bat` | Inicia oculto; se ja existe instancia, apenas informa status. |
| STOP | `Parar_Jarvis.bat` | Encerra todas as instancias verificadas deste `main.py`, incluindo launcher/filho do venv e orfaos sem PID. |
| RESTART | `Reiniciar_Jarvis.bat` | Executa STOP e START sob o mesmo lock por projeto. |
| STATUS | `Status_Jarvis.bat` | Mostra estado, PIDs verificados e situacao do `.jarvis.pid`. |
| LOGS | `Logs_Jarvis.bat` | Informa caminho e existencia de `jarvis.log`. |

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Jarvis.ps1 -Action status
Get-Content -LiteralPath .\jarvis.log -Tail 50 -Wait
```

Saida dos comandos: JSON pequeno; exit code 0 indica comando executado, 1 indica
falha. STATUS distingue `running` (PID confere), `starting_or_pid_missing`
(processo identificado sem PID correspondente), `stopped` e `identity_mismatch`
(arquivo aponta para outro processo). Processo vivo nao prova que microfone,
LLM ou orquestrador estao saudaveis; para isso existe o healthcheck #36.
LOGS nao imprime conteudo automaticamente. Logs podem conter dados pessoais.

START valida `.venv/Scripts/pythonw.exe` e usa caminho absoluto de `main.py`, com
diretorio de trabalho na raiz. Uma saida imediata e erro; imports/voz ainda podem
falhar depois dessa verificacao inicial. Consulte o log. Nao promete supervisao
externa apos crash nem sobrevivencia a reboot/logoff.

## Compatibilidade e parada

Os nomes Iniciar/Reiniciar foram preservados. Antes de adotar estes comandos,
feche a janela do **antigo Iniciar_Jarvis.bat com loop infinito**: essa janela
pode iniciar outra instancia depois de qualquer STOP, independentemente do PID.
Os comandos novos nao fecham consoles de outros programas.

O restart mantem a busca de multiplas instancias por linha de comando, necessaria
para o launcher/filho do venv. A busca agora exige invocacao Python de `main.py`
com caminho exato deste projeto. Para o formato antigo `main.py` relativo,
aceita apenas executavel explicitamente no venv deste projeto; esse venv deve
continuar reservado ao projeto. Um Python global com caminho relativo ambiguo
nao e encerrado sem evidencia adicional: o filho do redirector Python 3.12 e
reconhecido pelo parentesco com o launcher verificado, na mesma leitura de
processos e com data de criacao coerente. Se um processo legado relativo ja
perdeu esse pai e a identidade ficou ambigua, exige inspecao manual. Os novos
launchers sempre usam caminho absoluto, inclusive para reconhecer orfaos.
Invocacoes `-c`, `-m`, scripts de teste e outros projetos nao
sao selecionados por simplesmente conterem o nome do repositorio.

Antes de encerrar, revalida identidade e data de criacao com o handle aberto,
evitando atuar sobre PID reutilizado. STOP e forcado para recuperar processos
travados que disputam o microfone, como no script anterior; pode interromper
trabalho em andamento. O arquivo PID so e removido se inalterado e sem processo
estranho associado. Arquivo apontando para PID alheio bloqueia START: inspecione
o processo e corrija o arquivo manualmente, sem matar o processo alheio.

## Validacao

`tests/test_process_commands.py` executa processos Windows reais em projeto
descartavel com espacos no caminho: ciclo completo, partida idempotente e
concorrente, orfaos/duplicados, PID alheio, scripts parecidos e launcher antigo.
O `main.py` desse fixture apenas grava PID/log e espera; nao usa audio, APIs ou
credenciais. Ha job Windows proprio no CI. A suite Linux pula esses testes.
O Jarvis de uso pessoal nao e reiniciado como parte dos testes.
