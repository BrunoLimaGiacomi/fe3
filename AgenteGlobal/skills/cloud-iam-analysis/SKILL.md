---
name: cloud-iam-analysis
description: Use para analisar permissões efetivas, trust, herança, menor privilégio e caminhos de escalonamento em cloud IAM.
---

# Cloud IAM Analysis

## Diretrizes

- Identifique provedor, escopo, principal, papel/política, herança, condições e trust.
- Não trate inventário como prova de acesso efetivo quando faltarem denies, condições, trust ou herança.
- Flag administrador, owner, wildcard, credenciais long-lived, impersonation, pass-role, policy write e trust externo.
- Mapeie permissões para necessidade de negócio antes de recomendar remoção.
- Não remova acesso só por ausência de logs.

## Recomendação

- Prefira menor privilégio, grupos, federação, MFA/ativação privilegiada, credenciais curtas e revisão periódica.
- Para mudanças, proponha etapas, validação e rollback.
