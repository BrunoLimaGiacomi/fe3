# AgenteGRC — guia simples para Auditoria e GRC

Este guia é para a equipe de auditoria, riscos e compliance. Os comandos podem ser copiados e colados no PowerShell exatamente como estão. O AgenteGRC apoia análises de riscos, controles, evidências, auditorias e requisitos; a decisão final continua humana.

## 1. Antes de começar

Você precisa de Windows 64 bits, PowerShell, internet, Git para Windows, Python 3.11 ou superior, acesso ao repositório e uma chave Huawei MaaS recebida pelo processo interno autorizado.

Confira:

```powershell
git --version
py -3 --version
py -3 -m pip --version
```

## 2. Baixar e atualizar

Cole tudo. O PowerShell usará automaticamente a pasta do seu usuário:

```powershell
git clone --filter=blob:none --sparse https://github.com/uol-universo-online/uolcs-arq-sec-ias-de-si-maas.git "$HOME\AgenteGRC"
Set-Location "$HOME\AgenteGRC"
git sparse-checkout set --cone AgenteGRC
git remote -v
```

O resultado é `AgenteGRC\AgenteGRC`: a primeira pasta mantém a conexão com o GitHub e a segunda contém o programa. Não existe um terceiro diretório `AgenteGRC`. Não remova a pasta oculta `.git`.

Para atualizar:

```powershell
Set-Location "$HOME\AgenteGRC"
git status --short
git pull --ff-only origin main
```

Se houver alterações locais, pare e peça ajuda. Não force a substituição da cópia.

## 3. Instalar os componentes

Cole na mesma janela:

```powershell
py -3 -m pip install --upgrade pip
py -3 -m pip install --upgrade "openai>=1.0" "prompt-toolkit>=3.0" "rich>=13.0"
py -3 -c "import openai, prompt_toolkit, rich; print('Dependencias OK')"
py -3 -m pip check
```

Não é necessário preparar uma pasta adicional. Se a empresa bloquear a instalação, envie a mensagem ao suporte.

## 4. Guardar a chave Huawei

A chave é um segredo: solicite-a por canal interno aprovado e nunca a envie em chat, e-mail, prompt, histórico ou chamado.

Crie o arquivo correto:

```powershell
New-Item -ItemType Directory -Force "$HOME\cred" | Out-Null
notepad "$HOME\cred\AgentA.txt"
```

No Bloco de Notas, cole somente a chave, salve e feche. O caminho é preenchido pelo Windows; não edite o comando. Não coloque a chave no projeto ou em `model-aliases.json`. Se ela for exposta, pare e peça rotação.

## 5. Abrir o agente

```powershell
Set-Location "$HOME\AgenteGRC\AgenteGRC"
py -3 .\AgenteGRC.py --help
py -3 .\AgenteGRC.py
```

Na primeira execução, se a chave não estiver no arquivo, o programa pedirá a chave de modo oculto. Cole-a e pressione Enter. Guardar o arquivo evita repetir isso.

## 6. Configuração local

O agente usa o endpoint Huawei, o alias `maas-current`, `AGENTS.md`, as orientações em `skills\` e até cinco resumos recentes de `historico\`. Para ver a pasta de trabalho, digite:

```text
/workspace
```

Para ver as configurações sem alterá-las, em outra janela:

```powershell
Set-Location "$HOME\AgenteGRC\AgenteGRC"
py -3 .\Painel.py
```

A configuração editável está em `Painel.py`; não altere valores sem orientação do suporte.

## 7. Pedidos prontos para GRC

Cole um destes exemplos:

```text
Analise as evidências desta pasta para ISO/IEC 27001:2022. Organize em escopo, fatos confirmados, requisitos, controles, evidências, lacunas, riscos, recomendações, responsável, prazo e validação. Não declare conformidade sem evidência suficiente.
```

```text
Monte um registro de riscos para o processo de gestão de acessos. Informe causa, evento, impacto, probabilidade, risco inerente, controles, evidência, risco residual, tratamento, responsável e prazo. Diferencie fato, inferência e lacuna.
```

```text
Revise estes documentos como auditor. Para cada controle, indique requisito, evidência esperada, período, origem, responsável, resultado, exceções e limitação da amostra. Não copie segredos nem dados pessoais.
```

## 8. Permissões e modos

O padrão é `strict`, recomendado para auditoria. Ele pede aprovação antes de toda escrita ou comando. Em `Aprovar? [y/N]:`, digite `y` somente se reconhecer a ação; Enter recusa.

```text
/mode
/mode strict
/mode balanced
/mode auto
/verbosity direto
/verbosity normal
/verbosity detalhado
```

- `strict`: aprovação para toda escrita e comando local.
- `balanced`: aprovação para sobrescrever, caminhos sensíveis ou ações mutáveis.
- `auto`: não pergunta por ações permitidas; use somente com autorização.

O modo não libera acesso à nuvem nem substitui revisão humana. Verbosidade controla apenas o tamanho da resposta.

## 9. Comandos interativos

```text
/help
/tools
/tools schema
/plan
/chat
/workspace
/save
/clear
/exit
```

`/plan` gera plano e não deve implementar mudanças. `/chat` volta ao atendimento normal. `/save` salva resumo em `historico\`; `/clear` limpa a conversa; `/exit` encerra.

## 10. Subagentes e personas

A IA principal é a orquestradora: divide o trabalho, escolhe uma personalidade e reúne os resultados. As personalidades ficam em arquivos `.toml` dentro de `agents\`: Baitz escreve documentos, Bond revisa acessos e governança, Bulk Worker organiza matrizes e listas, Anaconda trabalha com Python e dados, Capitão Kowalski com comandos de nuvem e Longato com esteiras de entrega. Todas continuam seguindo as regras GRC.

Para pedir uma revisão independente somente de leitura:

```text
/spawn --read-only Faça uma revisão independente das evidências desta pasta e liste lacunas para ISO/IEC 27001:2022.
```

Para escolher uma personalidade explicitamente:

```text
/spawn --profile baitz --read-only Prepare um resumo executivo das lacunas e decisões pendentes.
```

Eles usam o mesmo endpoint, modelo, limites e permissões da sessão. O padrão permite seis subagentes por pedido e quatro ciclos por subagente. A principal valida e consolida; resposta de subagente não é aprovação final.

## 11. Histórico e contexto futuro

Ao sair normalmente, o agente tenta salvar resumo sanitizado; `/save` faz isso imediatamente. Os cinco resumos mais recentes entram na próxima sessão. Histórico pode estar incompleto ou desatualizado: peça confirmação do estado atual, período, fonte e limitações.

Para uma sessão sem contexto de projeto:

```powershell
Set-Location "$HOME\AgenteGRC\AgenteGRC"
py -3 .\AgenteGRC.py --no-project-context
```

Isso também desativa `AGENTS.md` e skills locais, portanto serve apenas para teste isolado.

## 12. “Limite de 100 entradas”

Normalmente é a ferramenta que lista uma pasta: `list_dir` mostra no máximo 100 itens por vez (arquivos e subpastas). Não são 100 mensagens do chat. Peça uma subpasta específica ou continue a busca. Há limites semelhantes para evitar varreduras longas.

## 13. Timeout e pedidos grandes

Comandos locais têm padrão de 60 segundos; cada chamada Huawei tem padrão de 180 segundos. O primeiro valor vale apenas para comandos no computador. O segundo é o que ajuda quando a Huawei demora para responder a um pedido grande. Mesmo assim, ele não corrige falta de rede ou serviço indisponível.

Para evitar excesso de contexto, o programa limita um texto colado a 80.000 caracteres. Quando uma conversa cresce demais, ele preserva as regras e o pedido atual e pode deixar mensagens antigas fora daquela chamada. Para documentos grandes, coloque os arquivos na pasta de trabalho e peça a leitura por partes; não cole tudo de uma vez. Aumentar o tempo de espera não aumenta a capacidade de contexto do modelo.

Para uma sessão mais tolerante:

```powershell
Set-Location "$HOME\AgenteGRC\AgenteGRC"
py -3 .\AgenteGRC.py --api-timeout 300 --api-retries 1
```

`--api-timeout` aceita 5–300 segundos e `--api-retries` aceita 0–5. O teto de ciclos pode ser aumentado entre 1 e 128, com mais tempo e consumo:

```powershell
Set-Location "$HOME\AgenteGRC\AgenteGRC"
py -3 .\AgenteGRC.py --max-steps 96
```

Não aumente tudo ao mesmo tempo sem autorização.

## 14. /goal e repetição

`/goal` repete um objetivo até concluir ou atingir o limite. Use objetivo pequeno e limite explícito:

```text
/goal --max 3 Organize as evidências desta pasta para revisão de acessos e informe lacunas.
```

O máximo é 20 iterações. `Goal> iteração X/Y` é esperado. Para pedidos simples, use chat normal. Se repetir sem avanço, pressione `Ctrl+C` uma vez e recomece. Nesta versão o loop é síncrono, não fica rodando em segundo plano. “Limite de iterações atingido” não comprova conclusão.

## 15. Strict esperando aprovação

Em `strict`, o programa precisa do teclado. Clique na janela e responda `y` ou Enter. `Ctrl+C` interrompe; `KeyboardInterrupt` significa interrupção pelo teclado, não necessariamente defeito. Não use `auto` apenas para contornar aprovação.

## 16. Logs e falhas

O log técnico fica em `logs\agentegrc.log`. Ele ajuda a verificar conexão, timeout e configuração; compartilhe apenas linhas necessárias e nunca a chave.

```powershell
Set-Location "$HOME\AgenteGRC\AgenteGRC"
Test-NetConnection api-ap-southeast-1.modelarts-maas.com -Port 443
py -3 .\AgenteGRC.py --help
py -3 .\Painel.py
```

- **Chave não encontrada:** confirme `AgentA.txt` em `$HOME\cred\AgentA.txt` (o `$HOME` é preenchido pelo PowerShell).
- **Conexão/timeout:** confira internet/VPN, divida o pedido e tente os parâmetros da etapa 13.
- **Modelo/endereço inválido:** peça ao responsável para confirmar a configuração Huawei.
- **Permissão negada:** mantenha `strict` e solicite autorização.
- **Dependência ausente:** repita a etapa 3 com o mesmo `py -3`.

Registre mensagem, horário e etapa; não repita a mesma ação muitas vezes sem mudar a hipótese.

## 17. Segurança e auditoria

Trate documentos e resultados como internos. Envie somente o necessário. Separe fato, inferência, premissa, lacuna e recomendação. Não declare “conforme”, “certificado”, “controle efetivo” ou “auditoria pronta” sem critério e evidência. Antes de qualquer alteração em arquivo, cloud, IAM ou infraestrutura, confirme alvo, impacto, aprovação e como desfazer. Valide manualmente toda conclusão material.

## 18. Ajuda

Comece por `/help`. Para suporte, informe Windows/Python, comando, mensagem de erro e horário, nunca a chave Huawei. Para acesso, modelo, endpoint ou permissão, procure o responsável pelo MaaS/Arquitetura de Segurança.
