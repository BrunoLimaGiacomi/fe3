"""Painel de configurações editáveis do AgenteGlobal.

Edite apenas os valores desta lista e reinicie o agente. Todas as constantes
abaixo são consumidas pelo runtime atual; limites de segurança, política,
escopo e aprovação continuam sendo aplicados pelo Core mesmo quando estes
valores são alterados.

Para conferir os valores sem iniciar o agente, execute ``Painel.py``. O
launcher ``AgenteGlobal\\bin\\agenteglobal.cmd`` usa o Python instalado para o
usuário, e ``setup.ps1`` instala as dependências sem exigir um venv.
"""

from __future__ import annotations

import sys
from typing import Final


# Execução e painel visual
# ------------------------
# Teto de ciclos modelo -> ferramenta em cada pedido.
DEFAULT_MAX_STEPS: int = 64

# Quantidade de tasks mostradas inicialmente no painel visual.
INITIAL_STEP_BUDGET: int = 8

# Quantas tasks são acrescentadas quando o orçamento visual se esgota.
STEP_BUDGET_INCREMENT: int = 8

# O painel começa com 8 linhas e pode revelar até mais 24 conforme a tarefa cresce.
DEFAULT_MAX_VISIBLE_TASKS: int = 32

# Orquestração: limites padrão de delegação e do workflow /goal.
DEFAULT_MAX_SUBAGENTS: int = 6
DEFAULT_SUBAGENT_MAX_STEPS: int = 64

# Timeout total de uma tarefa delegada. É separado do timeout de cada chamada MaaS.
DEFAULT_SUBAGENT_TIMEOUT_SECONDS: int = 1800
DEFAULT_GOAL_MAX_ITERATIONS: int = 5

# Timeouts e tolerância transitória.
DEFAULT_TIMEOUT_SECONDS: int = 60

DEFAULT_API_TIMEOUT_SECONDS: float = 180.0

# Retries adicionais após falha transitória da API. Um valor maior aumenta a
# tolerância, mas também pode prolongar uma falha real.
DEFAULT_API_RETRIES: int = 1

# Contexto local: histórico recente e limite de arquivos por busca.
DEFAULT_HISTORY_FILES: int = 5
DEFAULT_MAX_SEARCH_SCANNED_FILES: int = 5000


CONFIGURACOES: Final[tuple[tuple[str, object, str], ...]] = (
    ("DEFAULT_MAX_STEPS", DEFAULT_MAX_STEPS, "ciclos máximos por pedido; permitido: 1 a 128"),
    ("INITIAL_STEP_BUDGET", INITIAL_STEP_BUDGET, "tasks exibidas inicialmente; permitido: 1 a 128"),
    ("STEP_BUDGET_INCREMENT", STEP_BUDGET_INCREMENT, "tasks adicionadas por expansão; permitido: 1 a 128"),
    ("DEFAULT_MAX_VISIBLE_TASKS", DEFAULT_MAX_VISIBLE_TASKS, "tasks visíveis após expansão; permitido: 8 a 32"),
    ("DEFAULT_MAX_SUBAGENTS", DEFAULT_MAX_SUBAGENTS, "subagentes por pedido; permitido: 0 a 10"),
    ("DEFAULT_SUBAGENT_MAX_STEPS", DEFAULT_SUBAGENT_MAX_STEPS, "ciclos por subagente; permitido: 1 a 128"),
    (
        "DEFAULT_SUBAGENT_TIMEOUT_SECONDS",
        DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        "timeout total por subagente; permitido: 60 a 86400 s",
    ),
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
    print("Painel de configurações do AgenteGlobal")
    print("Edite as constantes deste arquivo e reinicie o agente.\n")
    for name, value, description in CONFIGURACOES:
        print(f"{name:<{name_width}} = {value!r}")
        print(f"  {description}")
    print("\nFixos do runtime: contexto operacional = 1.000.000 tokens; /deep = on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
