---
name: powershell-automation
description: Use para comandos, scripts e automações Windows/PowerShell executados pelo agente local.
---

# PowerShell Automation

## Diretrizes

- Prefira comandos nativos do PowerShell e parâmetros explícitos.
- Use `-LiteralPath` para caminhos controlados pelo usuário.
- Valide existência de arquivos, diretórios e comandos antes de mutações.
- Não monte comandos destrutivos por concatenação de strings.
- Para remoção, movimentação, alteração de ACL, registro, serviços ou nuvem, exija aprovação humana e explique impacto.
- Não imprima variáveis ou arquivos que possam conter segredos.
- Mantenha saída humana com cores quando útil, mas preserve dados estruturados quando solicitado.

## Validação

- Para comandos seguros, rode smoke tests pequenos.
- Para mutações, valide estado antes e depois.
- Para operações destrutivas, confirme alvo absoluto e rollback.
