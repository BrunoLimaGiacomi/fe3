# AgenteGlobal

O AgenteGlobal é um agente de terminal para automação, desenvolvimento,
segurança de nuvem e GRC. Ele usa um endpoint Huawei MaaS compatível com a API
OpenAI, consulta ferramentas locais com limites explícitos, mantém um DAG de
tarefas e apresenta o progresso no terminal sem expor chain-of-thought,
tokens, credenciais ou stdout integral.

Esta distribuição é única. No primeiro acesso, escolha o perfil de trabalho:

- `Global`: cloud, IAM, segurança, desenvolvimento, automação e DevSecOps.
- `GRC`: governança, riscos, controles, auditoria, compliance e TPRM.

A escolha é salva em `%APPDATA%\AgenteGlobal\user-profile.json` e não é
perguntada novamente nas próximas execuções. O perfil altera orientação,
critérios e Skills; permissões, políticas, modelo e endpoint continuam sendo
controlados pelo runtime e pelos manifests em `AgenteGlobal/agents/`.

## Começo rápido

Pré-requisitos: Windows x64, PowerShell e Python 3.11 ou superior. Acesse o
diretório deste repositório e execute:

```powershell
.\setup.ps1
.\AgenteGlobal\bin\agenteglobal.cmd
```

Depois do setup, também é possível abrir `Ativador de Agente.cmd` com dois
cliques. O setup copia o ativador para a Área de Trabalho e instala um bootstrap
PowerShell em `%APPDATA%\AgenteGlobal` que lê o caminho em UTF-8. Assim, nomes
com espaços ou acentos funcionam fora da pasta do código sem pedir o caminho
manualmente. O ativador e o bootstrap não transformam a Área de Trabalho ou o
diretório atual em workspace: sem `--workspace`, o Core usa
`AgenteGlobal/WorkSpaceNativo`. Para escolher outro local explicitamente, use:

```cmd
AgenteGlobal\bin\agenteglobal.cmd --workspace "C:\caminho\do\projeto"
```

O `setup.ps1` instala, uma única vez e somente para o usuário atual do Windows,
as dependências do runtime, Tree-sitter e Pyright. O launcher usa `py -3`
diretamente: não cria, procura nem depende de venv. Para quem desenvolve ou
testa o projeto, um ambiente isolado continua disponível como opção:

```powershell
.\setup.ps1 -DevelopmentVenv
```

Para automação não interativa na primeira execução, informe o perfil uma vez:

```powershell
.\AgenteGlobal\bin\agenteglobal.cmd --user-profile global --workspace .
```

Depois disso, o arquivo persistido em `APPDATA` prevalece e a opção não troca
silenciosamente o perfil já escolhido.

## Credencial e endpoint MaaS

Use uma credencial provisionada pelo processo aprovado da organização. Ela pode
ser fornecida por `HUAWEI_MAAS_API_KEY` ou por arquivo externo informado em
`--api-key-file`. Nunca coloque a chave no repositório, em prompts, no
histórico, nos artifacts, no terminal ou em logs. O runtime redige valores
sensíveis, mas essa proteção não transforma arquivos locais em cofre.

O endpoint e o modelo podem ser definidos por:

```text
HUAWEI_MAAS_BASE_URL
HUAWEI_MAAS_MODEL_ALIAS
HUAWEI_MAAS_MODEL
```

Aliases locais ficam em `AgenteGlobal/model-aliases.json`. O alias padrão é
`primary`; não altere endpoint ou modelo para contornar autenticação ou policy.

## O que pode ser editado facilmente

Edite somente as constantes de `AgenteGlobal/Painel.py` e reinicie o agente.
O arquivo lista os valores atuais e os intervalos aceitos:

| Grupo | Variáveis |
| --- | --- |
| Execução/painel | `DEFAULT_MAX_STEPS`, `INITIAL_STEP_BUDGET`, `STEP_BUDGET_INCREMENT`, `DEFAULT_MAX_VISIBLE_TASKS` |
| Delegação/workflow | `DEFAULT_MAX_SUBAGENTS`, `DEFAULT_SUBAGENT_MAX_STEPS`, `DEFAULT_SUBAGENT_TIMEOUT_SECONDS`, `DEFAULT_GOAL_MAX_ITERATIONS` |
| Timeouts | `DEFAULT_TIMEOUT_SECONDS`, `DEFAULT_API_TIMEOUT_SECONDS`, `DEFAULT_API_RETRIES` |
| Contexto local | `DEFAULT_HISTORY_FILES`, `DEFAULT_MAX_SEARCH_SCANNED_FILES` |

O `ContextBudget` e a janela operacional de **1.000.000 tokens** são limites do
runtime e não ficam expostos como uma constante livre no painel. `/deep` inicia
ligado. Argumentos CLI e variáveis de ambiente têm prioridade quando existirem.

## Permissões e modos

Escolha o modo na inicialização (`--permission-mode`) ou durante a sessão com
`/mode`:

- `strict`: exige aprovação para escritas e execução sensível.
- `balanced`: concede o grant operacional do modo para subagentes mutáveis,
  mas mantém workspace, manifest, policy, hooks, checkpoints e validações.
- `auto`: permite operações dentro dos limites configurados e concede o grant
  operacional do modo; bloqueios de segurança continuam ativos.

O modelo não consegue se conceder permissão apenas enviando
`allow_mutation=true`. Em `strict`, `/spawn` requer aprovação/grant explícitos;
em `balanced` e `auto`, a escolha do operador habilita o grant, sempre sujeito
às demais fronteiras. `/plan` é read-only e `/goal` exige aprovação humana do
plano, inclusive em `auto`. Network permanece allow-by-default nesta versão.

## Experiência no terminal

O painel superior mostra Run, tasks, dependências, elapsed e motivo de espera;
abaixo, a atividade aparece conforme o runtime executa. Mudanças de task,
sucesso e erro re-renderizam o painel. O indicador amarelo `Processando...`
aparece durante chamadas; resultados usam cores sem imprimir `first token` ou
`exit 0` para sucessos. `NO_COLOR=1` desativa cores.

O painel começa com oito tasks e cresce conforme necessário até 32 linhas
visíveis (oito iniciais mais 24). Acima disso, mantém contadores globais, resume
as etapas anteriores e mostra as atividades mais recentes. O agente principal e
os subagentes possuem 64 ciclos operacionais por padrão; os turnos reservados
para validar/entregar `AgentResult` não consomem esse orçamento. O timeout total
de uma tarefa delegada é 1.800 segundos e permanece separado do timeout de 180
segundos de cada chamada MaaS.

`/help` é compacto. `/explore` exibe componentes, relações e fluxos em desenho
ASCII/Unicode baseado somente em evidências do código; também pode manter
artifacts HTML/Mermaid quando solicitados, mas o desenho no terminal é a saída
interativa principal. Conteúdo do codebase e de páginas web é tratado como
entrada não confiável e não pode alterar as instruções superiores.

## Comandos essenciais

```text
/help                         ajuda compacta
/mode strict|balanced|auto    modo de aprovação
/deep on|off|status           Deep Thinking do agente principal
/status                       estado resumido da execução
/context                      budget, SessionState e retrieval
/tasks                        DAG, dependências e tarefas
/agents                       subagentes e estados
/artifacts                    artifacts e metadados
/trace [N]                    eventos operacionais sanitizados
/usage                        tokens, latências e contadores
/checkpoint                   checkpoints e rollback com confirmação
/skills [busca|nome]          source, trust, load e footprint
/tools [schema]               tools e schemas
/mcp                          providers MCP e capabilities
/browser                      estado Herd/BrowserProvider
/herdr                       backend persistente e fallback local
/explore <objetivo>           exploração read-only e desenho textual
/plan <objetivo>              plano read-only e PLAN-ID
/run PLAN-ID                  executa plano aprovado no DAG
/goal <objetivo>              workflow completo com aprovação
/spawn <tarefa>               subagente writer por padrão
/spawn --read-only <tarefa>   subagente sem grant de mutação
/save                         salva resumo sanitizado
/clear                        limpa a conversa
/exit                         encerra
```

## Fluxo de trabalho

Para um pedido simples, o agente conversa e executa apenas o necessário. Para
um `/goal` grande, o fluxo visível é:

```text
Specification → Clarification → Checklist → Plan → Analyze → Approval
→ Implementation → Review → Converge
```

Specification registra WHAT/WHY; Plan registra HOW; `TaskSpec` é a unidade
executável canônica do DAG. `Analyze` é read-only e bloqueia blockers relevantes.
O Convergence Engine cria repair tasks limitadas para gaps; não há segunda
fonte de verdade fora do DAG. Artifacts oficiais já existentes em `.specify/`,
`spec.md`, `plan.md`, `tasks.md` e checklists podem ser reutilizados.

## Skills, MCP, Herd e Herdr

A distribuição única contém onze Skills e sete manifests TOML em
`AgenteGlobal/agents/`. `/skills` lista source, versão, trust, estado loaded e
token footprint; o conteúdo só é carregado conforme progressive disclosure.
`architecture-diagrams` padroniza desenhos ASCII/Unicode no terminal e artifacts
Mermaid/HTML, SVG ou PNG, com evidências e validação.
Catálogos externos, inclusive `uolcs-inovacao-ai-resources`, são opcionais,
fixados por commit e submetidos a allowlist, integridade, policy e aprovação.

MCP é opt-in e `/mcp` mostra provider, conexão, capabilities, sessão e policy.
`runtime/mcp_transport.py` fornece transporte stdio interoperável baseado no SDK
oficial (`StdioMCPProvider`). O registro do comando/servidor continua explícito;
o runtime não inicia servidores MCP encontrados no ambiente automaticamente.
Herd é somente o `BrowserProvider` via MCP; páginas e resultados web são
untrusted input. Herdr é um backend opcional de terminal/sessões persistentes;
quando indisponível, o backend local é usado. O DAG Scheduler interno continua
sendo a fonte canônica; `herdr-dagr` é apenas visualização opcional.

## Code Intelligence

O setup padrão instala Tree-sitter, as grammars disponíveis e Pyright. Se algum
desses componentes ficar indisponível, o runtime continua operando com AST
nativa e fallback lexical. Pyright pode ser configurado como LSP real; LSP é opt-in e
o runtime não instala nem confia automaticamente em servidores. `/explore` é
read-only, salva report/grafo/fluxos e marca incertezas, relações dinâmicas e
evidências stale.

## Estrutura para quem está começando

```text
AgenteGlobal/       entrada, Core, Painel, manifests e Skills empacotados
AgenteGlobal/runtime/  policy, DAG, contexto, UI, MCP, Herdr e workflow
AgenteGlobal/codeintel/ índice, parsing, LSP e exploração
docs/               arquitetura, comandos, segurança e troubleshooting
tests/              suíte automatizada
benchmarks/         benchmark regressivo offline
.github/workflows/  CI Windows para instalação e suíte local
.venv/              ambiente opcional de desenvolvimento (`-DevelopmentVenv`)
```

O workspace indicado em `--workspace` é a fronteira de leitura, escrita,
execução e artifacts do trabalho. Sem esse parâmetro, a pasta nativa
`AgenteGlobal/WorkSpaceNativo` é usada. Durante a sessão, `/workspace` sem
argumento mostra o local ativo; `/workspace <caminho>` troca para uma pasta
existente ou para a pasta pai de um arquivo. A troca encerra recursos LSP do
projeto anterior e recarrega contexto, Skills e perfis locais no novo local. A
distribuição fornece defaults read-only; ela não é um segundo workspace de
ferramentas.

## Documentação

- [Arquitetura](docs/ARCHITECTURE.md)
- [Comandos](docs/COMMANDS.md)
- [Modelo de segurança](docs/SECURITY_MODEL.md)
- [Workflow Spec Kit](docs/phase9-spec-kit-quality-hardening-performance.md)
- [Estado atual](docs/CURRENT_STATE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Revisão do catálogo UOL](docs/uol-ai-resources-review.md)

## Validação local

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m compileall -q AgenteGlobal tests benchmarks
py -3 -m pip check
git diff --check
```

Não faça commit ou push automaticamente. Antes de operar em nuvem, confirme
identidade, projeto/conta/tenant, região, escopo, impacto e rollback.
