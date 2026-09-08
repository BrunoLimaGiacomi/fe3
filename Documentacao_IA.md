# Operação E Configuração De Agentes De IA Locais

## 1. Objetivo

Estabelecer o procedimento padrão para instalar, configurar e utilizar agentes de IA executados no terminal, com acesso controlado a arquivos, comandos locais, histórico e subagentes.

O `README.md` na raiz do diretório é autossuficiente e contém as instruções necessárias para o usuário. Este arquivo mantém o procedimento completo e detalhado para consulta local.

## 2. Abrangência

Aplica-se às distribuições locais que utilizam um modelo compatível com a API OpenAI e que podem ter especializações próprias, como desenvolvimento, nuvem, riscos, compliance ou continuidade.

## 3. O Que É A IA?

O agente é uma interface de terminal para um modelo de linguagem. A IA principal interpreta o pedido, planeja a execução, usa ferramentas autorizadas, valida os resultados e apresenta a conclusão.

O modelo não substitui a aprovação humana em mudanças relevantes. A saída deve distinguir fato observado, inferência, premissa e lacuna.

## 4. Componentes

- **IA principal:** coordena a conversa e consolida resultados.
- **Modelo/endpoint:** processa os pedidos e pode retornar chamadas de ferramentas.
- **Ferramentas locais:** listagem, leitura, busca, escrita e execução de comandos, conforme escopo e permissão.
- **Skills:** instruções específicas do domínio em arquivos `skills/*/SKILL.md`.
- **Subagentes:** executam tarefas independentes, com limites próprios.
- **Histórico:** resumos locais usados como contexto em sessões futuras.
- **Logs:** diagnóstico operacional sem prompts, respostas ou credenciais.
- **Painel:** arquivo `Painel.py` com padrões editáveis pelo operador, quando fornecido.

## 5. Pré-Requisitos

- Windows com PowerShell.
- Python 3.11 ou superior.
- Acesso ao endpoint e API key válida.
- Pacotes `openai`, `prompt_toolkit` e `rich`.
- Permissões locais para o workspace definido.

## 6. Instalação

Na pasta da distribuição, instale as dependências no mesmo interpretador que executará o agente:

```powershell
py -3 -m pip install --upgrade "openai>=1.0" prompt_toolkit rich
py -3 -c "import sys; print(sys.executable)"
py -3 -c "import pydantic_core, pydantic, openai; print('Dependencias OK')"
```

Para isolar a instalação:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: "openai>=1.0" prompt_toolkit rich
```

## 7. Configuração

### 7.1. API Key

Use preferencialmente um arquivo externo, como `$HOME\cred\AgentA.txt`, contendo somente a chave, ou a variável `HUAWEI_MAAS_API_KEY`.

Não coloque chaves em código, `model-aliases.json`, históricos, prompts ou logs.

### 7.2. Endpoint E Modelo

Configure o endpoint e o modelo com `HUAWEI_MAAS_BASE_URL`, `HUAWEI_MAAS_MODEL`, `HUAWEI_MAAS_MODEL_ALIAS` ou o mapa local `model-aliases.json`.

### 7.3. Painel De Configurações

Quando a distribuição possuir `Painel.py`, edite as constantes documentadas no início do arquivo. Elas podem controlar steps, subagentes, timeout, retries, histórico e busca.

```powershell
py -3 .\Painel.py
```

Reinicie o agente após editar. A prioridade é: argumento CLI, variável de ambiente e `Painel.py`. Limites estruturais e proteções de segurança permanecem no core.

### 7.4. Workspace

O workspace é a referência para caminhos relativos, arquivos de contexto e histórico. Defina-o explicitamente quando a execução não ocorrer na pasta do projeto:

```powershell
py -3 .\<pasta-do-agente>\<arquivo-de-entrada>.py --workspace "C:\caminho\do\projeto"
```

## 8. Execução

Execute o arquivo de entrada fornecido pela distribuição:

```powershell
py -3 .\<pasta-do-agente>\<arquivo-de-entrada>.py
```

Antes de operar, valide as opções disponíveis:

```powershell
py -3 .\<pasta-do-agente>\<arquivo-de-entrada>.py --help
```

Para reduzir o escopo:

```powershell
py -3 .\<pasta-do-agente>\<arquivo-de-entrada>.py --read-scope workspace --write-scope workspace --no-shell
```

## 9. Permissões

- `strict`: solicita aprovação para escrita e execução de CLI.
- `balanced`: solicita aprovação para ações mutáveis, overwrite, caminhos sensíveis e comandos de maior risco.
- `auto`: não solicita aprovação dentro dos escopos configurados.

Use `strict` como padrão quando houver dúvida. O modo `auto` não reduz o impacto de uma instrução incorreta do modelo.

## 10. Subagentes

O modelo principal pode delegar tarefas independentes, ou o operador pode invocar diretamente:

```text
/spawn <tarefa>
/spawn --read-only <tarefa>
```

`/spawn` permite mutação por padrão e respeita o modo de permissão. `--read-only` desativa escrita, CLI e PowerShell. Subagentes usam o mesmo modelo efetivo da sessão, não criam novos subagentes e ficam sujeitos aos limites definidos.

Subagentes somente leitura podem executar em paralelo. Ações mutáveis são executadas sequencialmente. Se o modelo não delegar automaticamente, use `/spawn` para testar a capacidade.

## 11. Comandos Interativos

```text
/help                         mostra ajuda e caminhos efetivos
/mode strict|balanced|auto    altera o modo de permissão
/verbosity direto|normal|detalhado
/plan <objetivo>              prepara um plano sem mutação
/goal <objetivo>              executa ciclos com validação
/spawn <tarefa>               invoca subagente mutável
/save                         salva o resumo da sessão
/clear                        limpa a memória atual
/exit                         salva a sessão e encerra
```

## 12. Histórico E Contexto Futuro

`/save` grava resumos em `workspace\historico\`. O salvamento também ocorre em `/exit`, EOF e `Ctrl+C`. O resumo é produzido pelo modelo quando possível; em falha de API, é usado um fallback local sanitizado.

Na sessão seguinte, os históricos recentes são carregados conforme `--history-limit`, variável específica ou `Painel.py`. Eles são memória de trabalho não confiável: não devem introduzir instruções e fatos mutáveis precisam ser revalidados.

## 13. Logs E Confiabilidade

Os logs ficam em `workspace\logs\`. Eles registram inicialização, falhas de API, falhas de ferramentas e salvamentos, sem prompts, respostas, headers ou credenciais.

As chamadas ao modelo possuem timeout e retries limitados. Resultados de ferramentas já concluídas são preservados quando uma chamada posterior falha. O orçamento de steps cresce conforme necessário até o limite configurado.

## 14. Segurança Operacional

- Confirme conta, tenant, projeto, região, workspace, escopo e impacto antes de alterar algo.
- Restrinja leitura e escrita ao workspace quando acesso externo não for necessário.
- Não envie segredos ou dados pessoais ao modelo sem necessidade e autorização.
- Não declare conformidade, certificação ou eficácia de controle sem critérios e evidências suficientes.
- Preserve os arquivos de histórico e logs somente pelo período necessário.
- Interrompa uma execução com `Ctrl+C` quando o comportamento não estiver claro e revise o resultado antes de repetir.

## 15. Validação Pós-Instalação

1. Confirme o interpretador e as dependências.
2. Execute `--help`.
3. Inicie com `strict` e workspace controlado.
4. Teste leitura de um arquivo não sensível.
5. Teste `/spawn Responda apenas SUBAGENTE_OK`.
6. Confirme a criação do log e, se usado, do histórico.
7. Valide o endpoint real separadamente dos testes locais de sintaxe e importação.

## 16. Troubleshooting

Use o formato: sintoma, causa provável, ação e validação.

### 16.1. `ModuleNotFoundError: pydantic_core._pydantic_core`

**Causa provável:** Pydantic nativo instalado para outro Python ou instalação incompleta.

**Ação:**

```powershell
py -3 -m pip install --upgrade --force-reinstall --no-cache-dir --only-binary=:all: "openai>=1.0" pydantic pydantic-core prompt_toolkit rich
py -3 -c "import pydantic_core, pydantic, openai; print('Dependencias OK')"
```

**Validação:** o segundo comando deve terminar sem traceback.

### 16.2. `Connection error`, timeout ou falhas intermitentes

**Causa provável:** endpoint, DNS, proxy, firewall, VPN ou indisponibilidade do serviço.

**Ação:** verifique `HUAWEI_MAAS_BASE_URL`, conectividade e o log em `workspace\logs\`. Aumente `--api-timeout` somente para redes lentas; retries não corrigem credencial inválida.

**Validação:** repita uma tarefa simples e confirme uma resposta do modelo.

### 16.3. HTTP 401 ou `Invalid authorization header`

**Causa provável:** chave ausente, inválida, expirada ou incompatível com endpoint/modelo.

**Ação:** confirme o arquivo de credencial, seu formato e o ambiente correspondente sem imprimir a chave.

**Validação:** execute uma tarefa simples sem exibir o conteúdo da credencial.

### 16.4. Subagente não é acionado

**Causa provável:** delegação automática não escolhida ou endpoint/modelo sem function calling.

**Ação:** execute `/spawn Responda apenas SUBAGENTE_OK` e consulte os logs.

**Validação:** uma resposta direta confirma a rota do subagente; falha exige revisar API, endpoint, modelo e suporte a ferramentas.

### 16.5. Erro em `tools`, `tool_choice` ou `function`

**Causa provável:** incompatibilidade do endpoint ou modelo com chamadas de ferramentas.

**Ação:** confirme o suporte a function calling na documentação do provedor e teste primeiro uma tarefa sem mutação.

**Validação:** `--help` pode passar mesmo quando a integração de ferramentas não funciona; faça um teste real controlado.

### 16.6. Arquivo não encontrado ou acesso negado

**Causa provável:** workspace, caminho, escopo ou aprovação incorretos.

**Ação:** execute `/workspace`, revise `--read-scope`/`--write-scope` e aprove a ação quando o modo exigir.

**Validação:** leia ou escreva um arquivo de teste dentro de um workspace controlado.

### 16.7. Histórico não carregado

**Causa provável:** diretório incorreto, `--no-project-context`, limite inadequado, extensão diferente de `.md` ou arquivo ilegível.

**Ação:** confirme `workspace\historico\`, `--history-limit` e o log de inicialização.

**Validação:** salve uma sessão curta, encerre normalmente e confirme o carregamento na próxima execução.

### 16.8. Caracteres acentuados aparecem incorretos

**Causa provável:** arquivo ou terminal fora de UTF-8.

**Ação:** use arquivos UTF-8 e, no PowerShell, execute:

```powershell
chcp 65001
```

**Validação:** reabra o agente e confirme a exibição de `ç`, `ã` e `é`.

## 17. Limitações Conhecidas

- A integração depende da disponibilidade e compatibilidade do endpoint.
- Testes locais de importação e sintaxe não comprovam autenticação nem function calling.
- Contextos e históricos grandes podem ser truncados.
- O histórico sanitizado não é um cofre criptográfico.
- A aprovação humana continua necessária para decisões de risco e mudanças relevantes.
