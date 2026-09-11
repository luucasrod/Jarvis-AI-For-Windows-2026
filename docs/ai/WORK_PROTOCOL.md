# WORK_PROTOCOL — Jarvis Orchestrator

Como Claude e Codex coordenam trabalho neste repositório durante a
implementação da fila de orquestração (46 issues, waves 0-6, ver os EPICs
#1-#7 no GitHub). Este documento é a fonte de verdade operacional — leia
antes de pegar qualquer Issue.

## 1. Dois estágios, não um

- **A) Pronto para servir de base** — implementado, testado, CI verde,
  integrado numa branch de integração de wave.
- **B) Aprovado para `main`** — passou por revisão cruzada independente e
  foi promovido no checkpoint da wave.

Uma Issue dependente **não precisa esperar `main`** se a dependência já
está em (A). Ela pode (e deve) continuar sobre a branch de integração da
wave. Só a promoção final para `main` exige (B).

Isso existe para que Claude e Codex trabalhem horas sem precisar estar
online ao mesmo tempo só para o outro revisar.

## 2. Branches de integração por wave

`integration/wave-0`, `integration/wave-1`, ... `integration/wave-6`.

Fluxo:

```
main
 -> integration/wave-X (acumula o estado testável da wave)
     -> branch individual da Issue (worktree próprio, PR próprio)
```

Cada Issue continua isolada: branch própria, worktree próprio, commits
próprios, PR próprio, evidência própria. A branch de integração só recebe
merges (`--no-ff`, preservando histórico) das branches de Issue já
validadas (testes+CI verdes), nunca código solto direto.

Quando a wave inteira estiver íntegra + revisões obrigatórias resolvidas +
testes integrados passando, ela é promovida (merge) para `main`. `main`
nunca é usada como atalho para destravar dependência — essa é a função da
`integration/wave-X`.

Não criar uma `develop` eterna — cada `integration/wave-X` é temporária,
existe só até a wave ser promovida.

## 3. Review Tasks são trabalho real

Toda implementação pronta para revisão gera uma **Review Task** (Issue no
GitHub, label `type:review`), nunca fica como "alguém revisa depois"
implícito. A Review Task referencia: Issue original, PR, implementador,
commits, critérios de aceite, testes, CI, arquivos principais, risco, e o
que ela desbloqueia.

Idempotência: antes de criar uma Review Task, procurar (`gh issue list
--label type:review`) se já existe uma para aquela Issue/PR — nunca
duplicar.

Labels de estado: `review:pending` -> `review:in-progress` ->
`review:passed` ou `review:changes-requested`.

Regra de revisor: quem implementou não revisa a própria implementação.
Claude implementa -> Codex revisa; Codex implementa -> Claude revisa.
Vale também para Issues FLEX.

## 4. Prioridade quando um agente escolhe o que fazer

- **P0 — Review que desbloqueia caminho crítico**: bloqueia promoção de
  wave, Issue SOLO importante, dependência crítica, deploy ou E2E. Fazer
  antes de começar implementação nova.
- **P1 — Review de checkpoint de wave**: wave pronta/quase pronta, limpar
  reviews pendentes daquela wave antes de acumular dívida.
- **P2 — Implementação READY de alta prioridade**: se reviews existentes
  não bloqueiam caminho crítico/checkpoint, pode implementar.
- **P3 — Review normal pendente**: antes de declarar "não tenho tarefa",
  procurar Review Tasks compatíveis.

Nunca vire tudo "review-first" nem tudo "review por último" — dependências
+ criticidade + checkpoint + risco decidem.

## 5. Regra de ociosidade

Nenhum agente declara "não tenho nada para fazer" sem checar, nessa ordem:
1. Issues READY para implementação;
2. Review Tasks pendentes (`type:review`, `review:pending`);
3. correções solicitadas em `review:changes-requested`;
4. bugs (`BUG_FOUND`) disponíveis;
5. Issues FLEX disponíveis.

Se não houver implementação adequada e houver review pendente compatível
-> faça a review.

## 6. Como executar uma Review Task

1. ler a Issue original; 2. ler a Review Task; 3. abrir o PR; 4. revisar o
diff; 5. verificar escopo (nada fora do declarado); 6. verificar critérios
de aceite; 7. verificar risco; 8. verificar testes; 9. rodar testes
relevantes quando possível; 10. verificar CI; 11. procurar regressões;
12. registrar o veredito.

**PASS**: marcar a Review Task concluída (`review:passed`), registrar
evidência, atualizar Issue/PR, desbloquear o que dependia dela.

**CHANGES_REQUESTED**: não corrigir tudo silenciosamente como revisor
(salvo trivial) — registrar o problema, devolver ao implementador
(`review:changes-requested`), ele corrige, nova validação depois. Não
duplicar a Review Task.

## 7. SOLO

Issues SOLO atuais: #11, #30, #35, #41, #45, #46. SOLO é modo de execução
(evita conflito), não é sinônimo de nenhum agente específico. Antes de uma
SOLO: verificar branch de integração da wave, verificar worktrees ativos,
garantir que não há edição concorrente na mesma área (main.py em especial
para #11/#30/#35).

## 8. `main` protegida

`main` só recebe checkpoints de wave já revisados. Nunca usar `main` como
branch intermediária para destravar uma dependência.
