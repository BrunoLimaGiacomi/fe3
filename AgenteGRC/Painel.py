"""Configurações locais e editáveis do AgenteGRC.

Edite somente os valores abaixo. O AgenteGRCCore importa estas constantes ao
iniciar. Argumentos da linha de comando e variáveis de ambiente, quando
existirem para a mesma opção, têm prioridade sobre este arquivo.

Execute ``py -3 Painel.py`` para listar os valores e seus efeitos.
"""

from __future__ import annotations

import sys
from typing import Final


# Quantidade máxima padrão de ciclos modelo -> ferramenta em cada pedido.
# Aumentar ajuda tarefas longas, mas pode elevar tempo e consumo da API.
DEFAULT_MAX_STEPS: int = 64

# Quantos steps aparecem inicialmente no painel visual. Se forem usados, o
# painel cresce em novos blocos até DEFAULT_MAX_STEPS.
INITIAL_STEP_BUDGET: int = 8

# Quantos novos steps são acrescentados quando o orçamento visual se esgota.
# Valores pequenos atualizam a previsão com mais frequência.
STEP_BUDGET_INCREMENT: int = 8

# Máximo padrão de subagentes que a IA principal pode iniciar por pedido.
# Aumentar permite mais frentes, mas também mais chamadas e possível custo.
DEFAULT_MAX_SUBAGENTS: int = 6

# Máximo padrão de ciclos de cada subagente. Valores maiores permitem análises
# mais profundas, com aumento de tempo e chamadas ao modelo.
DEFAULT_SUBAGENT_MAX_STEPS: int = 4

# Número padrão de iterações do comando /goal quando --max não for informado.
DEFAULT_GOAL_MAX_ITERATIONS: int = 5

# Timeout padrão, em segundos, de comandos locais executados pelas ferramentas.
# Aumentar ajuda comandos lentos; reduzir interrompe processos mais cedo.
DEFAULT_TIMEOUT_SECONDS: int = 30

# Timeout padrão, em segundos, de cada chamada ao endpoint MaaS.
# Aumentar tolera respostas lentas; reduzir detecta travamentos mais cedo.
DEFAULT_API_TIMEOUT_SECONDS: float = 45.0

# Número de novas tentativas após uma falha transitória da API.
# Aumentar melhora tolerância a instabilidade, mas prolonga uma falha real.
DEFAULT_API_RETRIES: int = 1

# Quantidade padrão de resumos recentes carregados de historico/.
# Mais históricos melhoram continuidade, mas aumentam contexto e consumo.
DEFAULT_HISTORY_FILES: int = 5

# Quantidade padrão de arquivos examinados por search_text.
# Aumentar amplia a busca e o tempo de varredura em workspaces grandes.
DEFAULT_MAX_SEARCH_SCANNED_FILES: int = 5000


CONFIGURACOES: Final[tuple[tuple[str, object, str], ...]] = (
    ("DEFAULT_MAX_STEPS", DEFAULT_MAX_STEPS, "ciclos máximos por pedido; permitido: 1 a 128"),
    ("INITIAL_STEP_BUDGET", INITIAL_STEP_BUDGET, "steps exibidos inicialmente; permitido: 1 a 128"),
    ("STEP_BUDGET_INCREMENT", STEP_BUDGET_INCREMENT, "steps adicionados por expansão; permitido: 1 a 128"),
    ("DEFAULT_MAX_SUBAGENTS", DEFAULT_MAX_SUBAGENTS, "subagentes por pedido; permitido: 0 a 10"),
    ("DEFAULT_SUBAGENT_MAX_STEPS", DEFAULT_SUBAGENT_MAX_STEPS, "ciclos por subagente; permitido: 1 a 15"),
    ("DEFAULT_GOAL_MAX_ITERATIONS", DEFAULT_GOAL_MAX_ITERATIONS, "iterações padrão do /goal; permitido: 1 a 20"),
    ("DEFAULT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, "timeout de comandos locais; permitido: 1 a 120 s"),
    ("DEFAULT_API_TIMEOUT_SECONDS", DEFAULT_API_TIMEOUT_SECONDS, "timeout da API; permitido: 5 a 300 s"),
    ("DEFAULT_API_RETRIES", DEFAULT_API_RETRIES, "retentativas adicionais; permitido: 0 a 5"),
    ("DEFAULT_HISTORY_FILES", DEFAULT_HISTORY_FILES, "históricos recentes; permitido: 1 a 20"),
    (
        "DEFAULT_MAX_SEARCH_SCANNED_FILES",
        DEFAULT_MAX_SEARCH_SCANNED_FILES,
        "arquivos examinados em buscas; permitido: 1 a 20000",
    ),
)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    name_width = max(len(name) for name, _, _ in CONFIGURACOES)
    print("Painel de configurações do AgenteGRC")
    print("Edite as constantes no início deste arquivo e reinicie o agente.\n")
    for name, value, description in CONFIGURACOES:
        print(f"{name:<{name_width}} = {value!r}")
        print(f"  {description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
