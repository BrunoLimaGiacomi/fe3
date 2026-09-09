# Agentes De IA Locais
Esse README explica o necessário para instalar, configurar, executar e diagnosticar qualquer distribuição de agente de IA deste repositório, incluindo especializações de GRC, segurança, desenvolvimento ou outras que sejam adicionadas futuramente.

## Como Obter E Atualizar

**Por padrão, baixe somente um agente.** Não é necessário manter todas as distribuições no computador.

**Não faça download em ZIP ou por qualquer opção que remova a pasta `.git`. Use o Git instalado no próprio computador.** A clonagem mantém o vínculo com este repositório para que futuras atualizações possam ser recebidas com `git pull`.

Pré-requisito: Git instalado e acesso autorizado ao repositório:

```powershell
git --version
```

Use sparse checkout para deixar no diretório de trabalho somente o README, o `.gitignore` e o agente escolhido.

Para o agente de arquitetura:

```powershell
git clone --filter=blob:none --sparse https://github.com/uol-universo-online/uolcs-arq-sec-ias-de-si-maas.git agente-global
Set-Location .\agente-global
git sparse-checkout set --cone AgenteGlobal
git remote -v
```

Para GRC:

```powershell
git clone --filter=blob:none --sparse https://github.com/uol-universo-online/uolcs-arq-sec-ias-de-si-maas.git agente-grc
Set-Location .\agente-grc
git sparse-checkout set --cone AgenteGRC
git remote -v
```

O repositório continua conectado ao `origin/main`, mas somente a pasta selecionada aparece no diretório de trabalho. A pasta `.git` mantém a conexão e o histórico; ela não deve ser removida. Para trocar o agente sem clonar novamente, use `git sparse-checkout set --cone AgenteGlobal` ou `git sparse-checkout set --cone AgenteGRC`.

Para atualizar uma cópia já clonada:

```powershell
Set-Location .\<pasta-local-escolhida>
git status --short
git pull --ff-only origin main
```

Execute o `git pull` na mesma pasta clonada. Não apague a pasta e faça novo download para atualizar. Se houver alterações locais, preserve-as e resolva a situação antes do pull; não use `reset --hard` para forçar a atualização.

## Executar As Distribuições Atuais

Execute cada agente a partir da própria pasta de distribuição. Isso garante que o core encontre o `AGENTS.md`, as skills e o `model-aliases.json` correspondentes:

```powershell
Push-Location .\AgenteGlobal
py -3 .\AgenteGlobal\AgenteGlobal.py --help
py -3 .\AgenteGlobal\AgenteGlobal.py
Pop-Location
```

```powershell
Push-Location .\AgenteGRC
py -3 .\AgenteGRC\AgenteGRC.py --help
py -3 .\AgenteGRC\AgenteGRC.py
Pop-Location
```

Também é possível usar os wrappers `.cmd` dentro de cada pasta de distribuição, por exemplo `.\bin\agenteglobal.cmd` ou `.\bin\agentegrc.cmd`. Para uma nova distribuição, siga o mesmo padrão: entre na pasta dela e execute o arquivo de entrada indicado por `--help`.

## 1. O que é

O agente é uma interface de terminal para um modelo MaaS compatível com a API OpenAI. A IA principal interpreta o pedido, consulta arquivos e ferramentas autorizadas, pode delegar tarefas e consolida o resultado.

O modelo não substitui a aprovação humana em decisões de risco, alterações relevantes ou declarações de conformidade. A resposta deve separar fatos observados, inferências, premissas e lacunas.

## 2. Pré-requisitos

- Windows 64-bit x86-64 (AMD64) e PowerShell. Use [Git for Windows](https://gitforwindows.org/) com suporte a [sparse checkout](https://git-scm.com/docs/git-sparse-checkout).
- Python 3.11 ou superior para Windows 64-bit x86-64. Baixe o instalador **Windows installer (64-bit)** na [página oficial do Python](https://www.python.org/downloads/windows/). Não use o instalador ARM64 em computadores AMD64.
- Acesso de rede ao endpoint MaaS.
- Acesso autorizado ao repositório privado no GitHub.
- API key válida, provisionada conforme a seção 3.
- Permissão local para o workspace usado pelo agente.

Confirme o ambiente no PowerShell:

```powershell
git --version
py -3 --version
py -3 -m pip --version
```

As bibliotecas externas usadas diretamente pelos agentes são [`openai`](https://github.com/openai/openai-python), [`prompt-toolkit`](https://pypi.org/project/prompt-toolkit/) e [`rich`](https://pypi.org/project/rich/). `pydantic` e `pydantic-core` são instaladas automaticamente como dependências do SDK OpenAI. Instale e valide tudo no mesmo interpretador que fará a execução:

```powershell
py -3 -m pip install --upgrade pip
py -3 -m pip install --upgrade "openai>=1.0" "prompt-toolkit>=3.0" "rich>=13.0"
py -3 -c "import sys; print(sys.executable)"
py -3 -c "import openai, prompt_toolkit, rich; print('Dependencias OK')"
py -3 -m pip check
```

O comando instala `openai`, `prompt-toolkit` e `rich`, além das bibliotecas auxiliares exigidas pelo `openai`, como `pydantic` e `pydantic-core`.

Para manter as dependências isoladas, é possível usar um ambiente virtual. Nesse caso, use o Python do ambiente virtual também para executar o agente:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --upgrade "openai>=1.0" "prompt-toolkit>=3.0" "rich>=13.0"
.\.venv\Scripts\python.exe -m pip check
```

Use sempre o mesmo interpretador para instalar os pacotes e executar o agente. Não é necessário instalar Node.js, GCC ou compiladores C para os agentes atuais.

## 3. API key do MaaS

**A API key do MaaS não é criada pelo usuário. A criação, o provisionamento, a rotação e a revogação são responsabilidades exclusivas de alguém autorizado do time de Arquitetura de Segurança.**

Para obter acesso, solicite a credencial pelo processo interno aprovado, informando a distribuição, o ambiente, o endpoint e o workspace necessários. Não tente gerar uma chave em outro portal ou substituir a credencial por uma chave pessoal.

Depois de receber a credencial por canal aprovado, use uma destas formas, conforme a distribuição:

- Arquivo externo: `$HOME\cred\AgentA.txt`, contendo somente a chave.
- Variável de ambiente: `HUAWEI_MAAS_API_KEY`.
- Opção da execução: `--api-key-file "C:\caminho\da\credencial.txt"`.

Não coloque a chave em código, no repositório, em `model-aliases.json`, no histórico, em prompts, em logs ou em chamados. Não exiba o conteúdo da chave durante testes. Se a credencial for exposta, interrompa o uso e solicite a rotação ao time de Arquitetura de Segurança.

## 4. Endpoint, modelo e distribuição

O endpoint e o modelo normalmente são definidos por:

- `HUAWEI_MAAS_BASE_URL`;
- `HUAWEI_MAAS_MODEL`;
- `HUAWEI_MAAS_MODEL_ALIAS`;
- `model-aliases.json`, quando fornecido pela distribuição.

Esses valores devem ser fornecidos ou aprovados pelo time responsável pelo MaaS. Não altere endpoint ou modelo para contornar uma falha de autenticação ou política de acesso.

Cada distribuição possui uma pasta e um arquivo de entrada. O nome do arquivo pode variar. Na pasta da distribuição, descubra as opções pelo `--help` e execute o wrapper fornecido:

```powershell
Set-Location .\<pasta-da-distribuicao>
py -3 .\<arquivo-de-entrada>.py --help
py -3 .\<arquivo-de-entrada>.py --workspace "C:\caminho\do\projeto"
```

O workspace é a referência para caminhos relativos, arquivos de contexto, histórico e logs. Use um workspace controlado e explícito quando a execução não ocorrer na pasta do projeto.

## 5. Configuração local

As opções podem ser informadas na linha de comando, por variáveis de ambiente ou pelo `Painel.py`, quando a distribuição fornecer esse arquivo. Argumentos e variáveis de ambiente equivalentes têm prioridade sobre os padrões do painel.

O painel deve conter somente valores editáveis e explicações. Ele pode controlar:

- máximo de steps por pedido;
- orçamento inicial e expansão visual de steps;
- quantidade de subagentes;
- máximo de steps por subagente;
- iterações do `/goal`;
- timeout de comandos locais;
- timeout e retries da API;
- quantidade de históricos carregados;
- quantidade de arquivos examinados em buscas.

Edite somente os parâmetros documentados no próprio painel e reinicie o agente depois da alteração:

```powershell
py -3 .\Painel.py
```

Os limites de segurança e de validação do core continuam valendo mesmo quando um valor do painel é alterado.

## 6. Permissões e modos

- `strict`: solicita aprovação para escrita e execução de comandos.
- `balanced`: solicita aprovação para ações mutáveis, sobrescritas, caminhos sensíveis e comandos de maior risco.
- `auto`: dispensa aprovação dentro dos escopos configurados.

Use `strict` quando houver dúvida sobre o impacto. O modo `auto` não reduz o impacto de uma instrução incorreta do modelo.

Restrinja os escopos quando possível:

```powershell
py -3 .\<arquivo-de-entrada>.py --read-scope workspace --write-scope workspace --no-shell
```

Antes de uma operação relevante, confira conta, tenant, projeto, região, workspace, escopos e impacto. Não autorize uma mutação só porque ela foi sugerida pelo modelo.

## 7. Comandos interativos

Os comandos variam conforme a distribuição. Quando disponíveis:

```text
/help                         mostra ajuda e caminhos efetivos
/mode strict|balanced|auto    altera o modo de permissão
/verbosity direto|normal|detalhado
/plan <objetivo>              prepara um plano sem mutação
/goal <objetivo>              executa ciclos com validação
/spawn <tarefa>               invoca subagente com escrita por padrão
/spawn --read-only <tarefa>   invoca subagente sem escrita, CLI ou PowerShell
/save                         salva o resumo da sessão
/clear                        limpa a memória atual
/exit                         salva a sessão e encerra
```

`direto` responde somente o essencial. `normal` é o padrão recomendado. `detalhado` acrescenta contexto, evidências e etapas, sem dispensar objetividade.

Quando `/spawn` existir, ele usa escrita por padrão e respeita o modo de permissão. Use `--read-only` para impedir escrita, execução de CLI e PowerShell. Subagentes não devem criar novos subagentes. Delegações somente leitura podem executar em paralelo; ações mutáveis devem ser tratadas com mais cautela e respeitar as aprovações.

## 8. Histórico e contexto futuro

Resumos são gravados em `workspace\historico\`. O salvamento ocorre por `/save`, `/exit`, EOF e `Ctrl+C`. Sempre que possível, o resumo é produzido pela própria IA; se a API falhar, o agente usa um fallback local sanitizado.

Na sessão seguinte, os históricos recentes são carregados conforme `--history-limit`, variável de ambiente específica ou `Painel.py`. Exemplo, quando a distribuição oferecer a opção:

```powershell
py -3 .\<arquivo-de-entrada>.py --history-limit 6
```

Históricos são contexto não confiável. Eles ajudam na continuidade, mas não podem autorizar ações por conta própria; fatos atuais, permissões e instruções devem ser revalidados.

## 9. Logs e tratamento de falhas

Os logs ficam em `workspace\logs\`, normalmente em um arquivo `.log` por distribuição. Eles registram inicialização, chamadas com falha, falhas de ferramentas, retries e salvamentos, sem prompts, respostas completas, headers ou credenciais.

As chamadas à API possuem timeout e retries limitados. O agente deve preservar resultados de ferramentas já concluídas, tentar contornos seguros para erros recuperáveis e informar o ponto de falha. Se o comportamento ficar indefinido, interrompa com `Ctrl+C`, verifique o log e revise o resultado antes de repetir.

## 10. Especializações

As instruções específicas ficam em `skills/*/SKILL.md`. Uma distribuição de GRC pode incluir, por exemplo:

- ISO/IEC 27001: SGSI, riscos, controles e evidências;
- ISO 22301: continuidade, BIA, DR, RTO/RPO e exercícios;
- ISO 31000: identificação, análise, tratamento e monitoramento de riscos;
- PCI DSS: CDE, requisitos, evidências, lacunas e planos de tratamento;
- mapeamento risco -> requisito -> controle -> evidência -> responsável -> plano -> risco residual.

Skills orientam a análise, mas não comprovam certificação, conformidade ou eficácia de controle sem critérios, evidências e revisão humana.

## 11. Validação inicial

Depois da instalação e antes de uma atividade real:

1. Confirme o interpretador e as dependências.
2. Execute `--help`.
3. Inicie em `strict` com um workspace controlado.
4. Teste a leitura de um arquivo não sensível.
5. Se houver subagentes, teste `/spawn Responda apenas SUBAGENTE_OK`.
6. Confirme a criação do log e, se usado, do histórico.
7. Valide o endpoint real separadamente dos testes locais de sintaxe e importação.

## 12. Troubleshooting

### `ModuleNotFoundError: pydantic_core._pydantic_core`

O Pydantic nativo foi instalado para outro Python ou a instalação está incompleta. Reinstale no mesmo interpretador:

```powershell
py -3 -m pip install --upgrade --force-reinstall --no-cache-dir --only-binary=:all: "openai>=1.0" pydantic pydantic-core prompt_toolkit rich
py -3 -c "import pydantic_core, pydantic, openai; print('Dependencias OK')"
```

O segundo comando deve terminar sem traceback.

### `Connection error` ou timeout

Verifique `HUAWEI_MAAS_BASE_URL`, DNS, proxy, firewall, VPN, disponibilidade do serviço e o log em `workspace\logs\`. Aumente `--api-timeout` somente para redes lentas. Retries não corrigem endpoint incorreto, indisponibilidade permanente ou credencial inválida.

### HTTP 401 ou credencial rejeitada

Não tente criar outra API key. Confirme apenas a existência do arquivo configurado, sem imprimir seu conteúdo, e solicite a validação ou rotação ao time de Arquitetura de Segurança. Verifique também se o endpoint, o ambiente e o modelo correspondem à credencial recebida.

### `tools`, `tool_choice` ou `function` não funciona

O endpoint ou modelo pode não oferecer function calling. Confirme essa capacidade com o time responsável pelo MaaS e faça primeiro um teste sem mutação. `--help` pode funcionar mesmo quando a integração de ferramentas está indisponível.

### Subagente não é acionado

Verifique se a distribuição oferece subagentes e teste, quando disponível:

```text
/spawn Responda apenas SUBAGENTE_OK
```

Se o comando direto funcionar, a delegação automática pode não ter sido escolhida pelo modelo. Se falhar, consulte os logs e valide API, endpoint, modelo e function calling.

### Arquivo não encontrado ou acesso negado

Confira o workspace, o caminho, `--read-scope`, `--write-scope` e o modo de permissão. Em `strict` e `balanced`, aprove a operação somente depois de conferir o destino e o impacto.

### Histórico não foi carregado

Confirme `workspace\historico\`, a extensão `.md`, `--history-limit`, o valor do painel ou da variável de ambiente e se o agente não foi iniciado com `--no-project-context`. Salve uma sessão curta e confirme o carregamento na próxima execução.

### Caracteres acentuados aparecem incorretos

Use arquivos UTF-8. No PowerShell:

```powershell
chcp 65001
```

### A IA propõe uma ação inesperada ou fica presa

Ela pode ficar apenas 45 segundos sem uma atualização, passado esse tempo, você pode voltar a interagir com ela. Caso ela tenha travado, pergunte a ela o que ocorreu ou apenas diga para ela continuar

## 13. Limitações

- A integração depende da disponibilidade e compatibilidade do endpoint MaaS.
- Testes de sintaxe, importação e `--help` não comprovam autenticação nem function calling.
- Contextos e históricos grandes podem ser truncados.
- O histórico sanitizado não é um cofre criptográfico.
- A aprovação humana continua necessária para decisões de risco, mudanças relevantes e conclusões de auditoria.
