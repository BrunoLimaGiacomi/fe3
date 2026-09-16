# Resumo do Workspace

## Visão Geral

**Workspace:** `MaaS-repo - Copia`
**Total de entradas no topo:** 13 (8 diretórios + 5 arquivos)

## Estrutura de Diretórios

| Diretório | Descrição |
|---|---|
| `.agenteglobal/` | Estado interno do agente (validações, cache) |
| `.git/` | Controle de versão Git |
| `.venv/` | Ambiente virtual Python (excluído da análise) |
| `AgenteGlobal/` | Código-fonte principal do agente |
| `benchmarks/` | Scripts de benchmark e regressão |
| `docs/` | Documentação |
| `logs/` | Logs de execução |
| `tests/` | Suíte de testes |

## Arquivos Raiz

| Arquivo | Tamanho |
|---|---|
| `.gitignore` | 228 B |
| `README.md` | 9.733 B |
| `requirements-codeintel.txt` | 222 B |
| `requirements-poc.txt` | 90 B |
| `setup.ps1` | 2.391 B |

## Arquivos Python (136 no total, excluindo `.venv` e `.git`)

### Por categoria

| Localização | Quantidade | Descrição |
|---|---|---|
| `AgenteGlobal/` (raiz) | 4 | Entry points: `AgenteGlobal.py`, `AgenteGlobalCore.py`, `Painel.py`, `query_iam_logs.py` |
| `AgenteGlobal/codeintel/` | 8 | Code Intelligence: indexação, AST, LSP, admission control |
| `AgenteGlobal/llm/` | 8 | Camada LLM: Huawei MaaS, reasoning, capabilities, contracts |
| `AgenteGlobal/runtime/` | 49 | Runtime principal: scheduler, reviewer, delegation, context, tools, policies |
| `benchmarks/` | 2 | Benchmarks de regressão e smoke test |
| `tests/` | 41 | Suíte de testes (fases 2–11 + integração) |
| `.agenteglobal/validation/` | 24 | Scripts de validação phase11 (tree-sitter, LSP, cloud) |

### Destaques do núcleo (`AgenteGlobal/runtime/`)

- **Orquestração:** `scheduler.py`, `planning.py`, `delegation.py`, `reviewer.py`
- **Contexto:** `context_engine.py`, `context_budget.py`, `retrieval.py`, `ingestion.py`
- **Segurança:** `mutation_policy.py`, `operation_safety.py`, `security_text.py`, `cloud_scope.py`
- **Governança:** `tool_governance.py`, `skill_governance.py`, `policies.py`, `hooks.py`
- **UI/Terminal:** `terminal_ui.py`, `terminal_backends.py`, `status_views.py`
- **Estado:** `session_state.py`, `operational_state.py`, `checkpoints.py`, `convergence.py`

## Resumo Executivo

O workspace é o repositório do **AgenteGlobal**, um agente CLI local para DevSecOps com:
- Núcleo em Python com 136 arquivos de projeto
- Arquitetura modular: `runtime/` (49 módulos), `llm/` (8), `codeintel/` (8)
- 41 testes cobrindo fases 2 a 11
- Integração com Huawei MaaS (LLM) e Code Intelligence (tree-sitter/LSP)
- Suporte a subagentes, skills, delegation e governance
