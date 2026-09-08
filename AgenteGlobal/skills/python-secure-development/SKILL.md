---
name: python-secure-development
description: Use para criar, modificar ou depurar Python de automação, CLI, clientes de API e tooling operacional com configuração segura.
---

# Python Secure Development

## Diretrizes

- Use Python 3.11+ e `pathlib` para caminhos.
- Separe CLI, configuração, chamadas externas e lógica de negócio quando isso melhorar manutenção.
- Use type hints em interfaces relevantes.
- Trate erros específicos e preserve contexto útil.
- Nunca hardcode, imprima ou registre segredos.
- Configure timeouts explícitos para rede e subprocessos.
- Prefira `argparse` para CLI sem dependência adicional.
- Use UTF-8 para leitura e escrita de texto.

## Validação

- Valide sintaxe com `ast.parse` ou `py_compile`.
- Teste caminhos sem credencial e com credencial dummy quando possível.
- Para chamadas reais, faça smoke test mínimo e não exponha payloads sensíveis.
- Reporte comportamento verificado, arquivos alterados e riscos residuais.
