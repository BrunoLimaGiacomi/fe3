from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from getpass import getpass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError
from pydantic import SecretStr, ValidationError

from codeintel import CodeIntelligenceRuntime, ExplorationDepth, ExplorationPreparation, load_lsp_providers
from llm.base import ModelAdapter, ModelRequest
from llm.capabilities import load_capabilities_snapshot
from llm.contracts import ModelResponse, ToolCall
from llm.huawei_maas import (
    DEFAULT_BASE_URL,
    DEFAULT_CAPABILITIES_FILE,
    DEFAULT_MODEL,
    HuaweiMaaSAdapter,
    HuaweiMaaSConfig,
)
from llm.reasoning import ReasoningMode
from Painel import (
    DEFAULT_API_RETRIES,
    DEFAULT_API_TIMEOUT_SECONDS,
    DEFAULT_GOAL_MAX_ITERATIONS,
    DEFAULT_HISTORY_FILES,
    DEFAULT_MAX_VISIBLE_TASKS,
    DEFAULT_MAX_SEARCH_SCANNED_FILES,
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_SUBAGENTS,
    DEFAULT_SUBAGENT_MAX_STEPS,
    DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    INITIAL_STEP_BUDGET,
    STEP_BUDGET_INCREMENT,
)
from runtime.config import (
    AgentConfig,
    DEFAULT_MODEL_ALIASES,
    MAX_MODEL_ALIAS_DEPTH,
    MAX_MODEL_ALIAS_FILE_BYTES,
    load_model_aliases,
    resolve_model_name,
)
from runtime.agent_registry import AgentManifest, AgentRegistry, CapabilityMatcher, ManifestError
from runtime.artifacts import ArtifactMetadata, ArtifactStore, externalize_tool_result
from runtime.checkpoints import CheckpointConflictError, CheckpointError, CheckpointManager
from runtime.context_budget import BudgetCategory, ContextBudget
from runtime.context_engine import ContextEngine, LargeInputContext, ingest_oversized_user_input
from runtime.context_window import (
    PromptTooLargeError,
    close_oversized_turn,
    message_size_chars,
    prepare_messages as prepare_token_bounded_messages,
    prepare_messages_by_chars,
    system_state_key,
)
from runtime.cloud_scope import CloudScopeResolver, is_cloud_command_mutating
from runtime.convergence import ConvergenceEngine, ConvergenceLimitExceeded
from runtime.contracts import (
    Approval,
    ApprovalDecision,
    AgentError,
    AgentErrorCode,
    AgentResult,
    AgentResultStatus,
    Goal,
    GoalLifecycleStatus,
    Plan,
    PlanLifecycleStatus,
    ReviewDecision,
    ReviewResult,
    ScopeExpansion,
    ScopeExpansionStatus,
    TaskLifecycleStatus,
    TaskLimits,
    TaskSpec,
    UserQuestion,
    UserQuestionStatus,
)
from runtime.delegation import DelegationAction, DelegationAdmissionController
from runtime.events import (
    DEFAULT_MAX_PARALLEL_TOOLS,
    EventBus,
    can_parallelize_tools,
)
from runtime.metrics import LocalMetricsCollector, LocalMetricsStore
from runtime.mcp import MCPRegistry
from runtime.mutation_policy import resolve_mutation_grant
from runtime.observers import StructuredEventLogger, TerminalEventConsumer
from runtime.operational_state import OperationalState, context_budget_payload
from runtime.operation_safety import is_destructive_command, is_mutating_command, is_sensitive_path, is_unsafe_command
from runtime.planning import PlanNotFoundError, PlanStore, PlanStoreError
from runtime.process import ProcessRequest, ProcessRunner
from runtime.run_journal import (
    RecoveryDisposition,
    RunCorruptError,
    RunJournal,
    RunLifecycleStatus,
    RunNotFoundError,
    RunPlanMismatchError,
    SideEffectState,
)
from runtime.prompt_context import (
    build_main_system_prompt,
    build_project_context as assemble_project_context,
    build_subagent_context as assemble_subagent_context,
    build_subagent_messages as assemble_subagent_messages,
    read_context_file as load_context_file,
    read_saved_history_context as load_saved_history_context,
)
from runtime.result_protocol import (
    SUBMIT_AGENT_RESULT_FUNCTION,
    AgentResultFunctionProtocol,
    AgentResultProtocolError,
    FunctionCall,
    ResultProtocolErrorCode,
    ResultProtocolFailure,
)
from runtime.reviewer import (
    SUBMIT_REVIEW_RESULT_FUNCTION,
    RepairLimitExceeded,
    ReviewProtocolError,
    ReviewResultFunctionProtocol,
    repair_closure,
)
from runtime.retrieval import RetrievalStrategy
from runtime.reliability import is_retryable_api_error, retry_delay_seconds
from runtime.resource_paths import packaged_resources, resolve_resource_path
from runtime.scheduler import DAGScheduler, SchedulerResult, TaskExecutor
from runtime.security_text import (
    exception_chain_summary,
    is_sensitive_key_name,
    redact_cli_args,
    redact_command_text,
    redact_sensitive_text,
    safe_subprocess_env,
    truncate_single_line,
    truncate_text,
)
from runtime.skills import SkillRegistry, SkillRegistryError
from runtime.status_views import (
    estimate_skill_tokens,
    render_agents,
    render_artifacts,
    render_context,
    render_overview,
    render_skills,
    render_spec,
    render_tasks,
    render_trace,
    render_usage,
    section,
)
from runtime.spec_kit import SpecKitAdapter, SpecKitCategory, SpecKitContext, SpecKitError
from runtime.spec_workflow import (
    AnalyzeResult,
    SpecWorkflowContext,
    SpecWorkflowCoordinator,
    SpecWorkflowStage,
    emit_spec_workflow_stage,
)
from runtime.tool_governance import RuntimeGovernance
from runtime.tool_arguments import (
    AskUserQuestionArguments,
    DelegateTaskArguments,
    ListDirArguments,
    MAX_ARTIFACT_TOOL_READ_BYTES,
    MAX_PROFILE_NAME_CHARS,
    MAX_SUBAGENT_TASK_CHARS,
    ReadArtifactArguments,
    ReadFileArguments,
    RequestScopeExpansionArguments,
    RetrieveContextArguments,
    RunCliArguments,
    RunPowerShellArguments,
    SearchTextArguments,
    SpawnSubagentArguments,
    SubmitPlanArguments,
    TOOL_ARGUMENT_MODELS,
    ToolArguments,
    WriteFileArguments,
    parse_manual_spawn_options,
    validate_tool_arguments,
)
from runtime.terminal_backends import HerdrTerminalBackend
from runtime.terminal_task_board import LoadingIndicator, TerminalTask, TerminalTaskBoard
from runtime.terminal_text import plain_terminal_markdown
from runtime.terminal_tool_views import (
    describe_tool_activity,
    infer_assistant_content_style,
    render_exploration_report,
    summarize_tool_result,
)
from runtime.terminal_ui import TerminalUI
from runtime.user_profile import load_or_select_profile
from runtime.workflow_support import (
    aggregate_delegated_task_result,
    build_plan_prompt,
    create_plan_approval,
    format_execution_review,
    format_plan_summary,
    parse_goal_command,
    resolve_plan_approval,
    scope_expansion_replanning_objective,
)

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
except ImportError:
    ANSI = None
    Completer = None
    Completion = None
    PromptSession = None

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
except ImportError:
    box = None
    Console = None
    Panel = None
    Table = None
    Text = None
    Theme = None

API_KEY_ENV = "HUAWEI_MAAS_API_KEY"
API_KEY_FILE_ENV = "HUAWEI_MAAS_API_KEY_FILE"
BASE_URL_ENV = "HUAWEI_MAAS_BASE_URL"
MODEL_ENV = "HUAWEI_MAAS_MODEL"
MODEL_ALIAS_ENV = "HUAWEI_MAAS_MODEL_ALIAS"
MODEL_ALIAS_FILE_ENV = "HUAWEI_MAAS_MODEL_ALIAS_FILE"
LSP_CONFIG_ENV = "AGENTEGLOBAL_LSP_CONFIG"
AGENT_NAME = "AgenteGlobal"
AGENT_SLUG = "agenteglobal"
PERMISSION_MODE_ENV = "AGENTEGLOBAL_PERMISSION_MODE"
READ_SCOPE_ENV = "AGENTEGLOBAL_READ_SCOPE"
WRITE_SCOPE_ENV = "AGENTEGLOBAL_WRITE_SCOPE"
VERBOSITY_MODE_ENV = "AGENTEGLOBAL_VERBOSITY"
HISTORY_LIMIT_ENV = "AGENTEGLOBAL_HISTORY_LIMIT"
PROFILES_DIR_ENV = "AGENTEGLOBAL_PROFILES_DIR"

DEFAULT_API_KEY_FILE = Path.home() / "cred" / "AgentA.txt"
DEFAULT_MODEL_ALIAS = "primary"
DEFAULT_MODEL_ALIAS_FILE = "model-aliases.json"
DEFAULT_PROFILES_DIR = "agents"
MAX_ALLOWED_STEPS = 128
MAX_API_RETRIES = 5
DEFAULT_MAX_REPAIRS = 2
MAX_REPAIR_ATTEMPTS = 10
REVIEW_PROTOCOL_REPAIRS = 1
HISTORY_SUMMARY_TIMEOUT_SECONDS = 15.0
MAX_API_KEY_FILE_BYTES = 10_000
MAX_PROFILE_FILE_BYTES = 40_000
MAX_PROFILE_INSTRUCTIONS_CHARS = 24_000
MAX_PROFILE_COUNT = 20
MAX_PROFILE_DESCRIPTION_CHARS = 500
MAX_PROFILE_TOTAL_INSTRUCTIONS_CHARS = 80_000
MAX_USER_INPUT_CHARS = 80_000
MAX_API_MESSAGE_CHARS = 300_000
MAX_TOOL_OUTPUT_CHARS = 12000
MAX_CONTEXT_FILE_BYTES = 80_000
MAX_CONTEXT_TOTAL_BYTES = 240_000
MAX_HISTORY_TRANSCRIPT_CHARS = 60_000
MAX_HISTORY_FILE_BYTES = 40_000
MAX_HISTORY_FILES = 20
MAX_HISTORY_SUMMARY_CHARS = 24_000
MAX_READ_BYTES = 250_000
MAX_WRITE_BYTES = 1_000_000
HISTORY_DIR_NAME = "historico"
LOG_DIR_NAME = "logs"
LOG_FILE_MAX_BYTES = 1_000_000
LOG_FILE_BACKUP_COUNT = 3


def validate_panel_configuration() -> None:
    integer_settings = (
        ("DEFAULT_MAX_STEPS", DEFAULT_MAX_STEPS, 1, MAX_ALLOWED_STEPS),
        ("INITIAL_STEP_BUDGET", INITIAL_STEP_BUDGET, 1, MAX_ALLOWED_STEPS),
        ("STEP_BUDGET_INCREMENT", STEP_BUDGET_INCREMENT, 1, MAX_ALLOWED_STEPS),
        ("DEFAULT_MAX_VISIBLE_TASKS", DEFAULT_MAX_VISIBLE_TASKS, INITIAL_STEP_BUDGET, 32),
        ("DEFAULT_MAX_SUBAGENTS", DEFAULT_MAX_SUBAGENTS, 0, 10),
        ("DEFAULT_SUBAGENT_MAX_STEPS", DEFAULT_SUBAGENT_MAX_STEPS, 1, MAX_ALLOWED_STEPS),
        ("DEFAULT_SUBAGENT_TIMEOUT_SECONDS", DEFAULT_SUBAGENT_TIMEOUT_SECONDS, 60, 86_400),
        ("DEFAULT_GOAL_MAX_ITERATIONS", DEFAULT_GOAL_MAX_ITERATIONS, 1, 20),
        ("DEFAULT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 1, 120),
        ("DEFAULT_API_RETRIES", DEFAULT_API_RETRIES, 0, MAX_API_RETRIES),
        ("DEFAULT_HISTORY_FILES", DEFAULT_HISTORY_FILES, 1, MAX_HISTORY_FILES),
        ("DEFAULT_MAX_SEARCH_SCANNED_FILES", DEFAULT_MAX_SEARCH_SCANNED_FILES, 1, 20_000),
    )
    errors: list[str] = []
    for name, value, minimum, maximum in integer_settings:
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{name} precisa ser inteiro")
        elif value < minimum or value > maximum:
            errors.append(f"{name} precisa estar entre {minimum} e {maximum}")

    if (
        isinstance(DEFAULT_API_TIMEOUT_SECONDS, bool)
        or not isinstance(DEFAULT_API_TIMEOUT_SECONDS, (int, float))
        or DEFAULT_API_TIMEOUT_SECONDS < 5
        or DEFAULT_API_TIMEOUT_SECONDS > 300
    ):
        errors.append("DEFAULT_API_TIMEOUT_SECONDS precisa estar entre 5 e 300")

    if errors:
        raise ValueError("Configuração inválida em Painel.py: " + "; ".join(errors))


validate_panel_configuration()

PERMISSION_MODES = {
    "strict": "pede aprovação para toda escrita e toda execução de CLI",
    "balanced": "pede aprovação para overwrite, caminhos sensíveis e comandos destrutivos ou mutáveis",
    "auto": "não pede aprovação para escrita, CLI ou leitura sensível dentro dos escopos configurados",
}
VERBOSITY_MODES = {
    "direto": "responde no menor tamanho útil, com conclusão e próximos passos essenciais",
    "normal": "responde de forma objetiva, com contexto suficiente e sem excesso",
    "detalhado": "responde com mais contexto, critérios e ressalvas relevantes, sem alongar artificialmente",
}
READ_SCOPES = {
    "system": "leitura, listagem e busca aceitam caminhos absolutos fora do workspace",
    "workspace": "leitura, listagem e busca ficam restritas ao workspace",
}
WRITE_SCOPES = {
    "system": "escrita aceita caminhos absolutos e referências fora do workspace conforme /mode",
    "workspace": "escrita fica restrita ao workspace",
}
PATH_REFERENCE_VALUES = (
    "workspace",
    "home",
    "desktop",
    "downloads",
    "documents",
    "onedrive",
    "pictures",
    "music",
    "videos",
    "temp",
)
API_KEY_ASSIGNMENT_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "huawei_maas_api_key",
    "maas_api_key",
    "openai_api_key",
    "token",
}

WHITE = "\033[37m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
GRAY = "\033[90m"
RESET = "\033[0m"

ANSI_BY_STYLE = {
    "white": WHITE,
    "cyan": CYAN,
    "green": GREEN,
    "yellow": YELLOW,
    "red": RED,
    "gray": GRAY,
}
RICH_STYLE_BY_NAME = {
    "white": "agent.neutral",
    "cyan": "agent.primary",
    "green": "agent.success",
    "yellow": "agent.info",
    "red": "agent.error",
    "gray": "agent.muted",
}
RICH_THEME = {
    "agent.neutral": "white",
    "agent.primary": "cyan",
    "agent.success": "green",
    "agent.info": "yellow",
    "agent.error": "red",
    "agent.muted": "bright_black",
}
_RICH_CONSOLE: Any | None = None
LOGGER = logging.getLogger(AGENT_SLUG)
LOGGER.addHandler(logging.NullHandler())
DIAGNOSTIC_LOG_PATH: Path | None = None
_TASK_BOARD_CONTEXT: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "agenteglobal_task_board",
    default=None,
)
_APPROVAL_PROMPT_LOCK = threading.Lock()

Message = dict[str, Any]

SLASH_COMMANDS = {
    "/help": "Mostra ajuda local",
    "/tools": "Lista ferramentas expostas ao modelo",
    "/tools schema": "Mostra o schema JSON das ferramentas",
    "/mode": "Mostra ou altera modo de permissões",
    "/verbosity": "Mostra ou altera verbosidade: direto, normal ou detalhado",
    "/deep": "Liga/desliga Deep Thinking só do agente principal: /deep on|off|status",
    "/plan": "Somente planeja (Deep/read-only), pergunta e salva PLAN-ID; não executa",
    "/explore": "Explora arquitetura/fluxos read-only com índice, evidências, LSP e artifacts",
    "/run": "Executa DAG, locks, workers, review e repair de um PLAN-ID aprovado",
    "/resume": "Retoma RUN-ID persistido sem repetir tasks concluídas",
    "/chat": "Volta para o chat padrão",
    "/goal": "Plan + aprovação humana + DAG + Reviewer + repair limitado + final",
    "/status": "Resumo operacional de Run, Goal, Spec, DAG, contexto e espera",
    "/spec": "Mostra os estágios do workflow Spec Kit",
    "/spawn": "Delegação manual writer por padrão: /spawn [--profile nome] [--read-only] <tarefa>",
    "/context": "Mostra orçamento, SessionState e índice local sem exibir conteúdo",
    "/tasks": "Mostra DAG, dependências e estados das tarefas observadas",
    "/agents": "Mostra agentes e o motivo de eventual espera",
    "/trace": "Mostra o trace operacional sanitizado da sessão",
    "/usage": "Mostra tokens, latências e contadores locais",
    "/checkpoint": "Lista checkpoints; rollback exige confirmação explícita",
    "/artifacts": "Lista metadata dos artifacts locais recuperáveis",
    "/skills": "Lista skills por metadata; carrega conteúdo somente quando selecionado",
    "/mcp": "Mostra providers MCP, conexão, capabilities e policy",
    "/browser": "Mostra o BrowserProvider/Herd sem duplicar sua interface",
    "/herdr": "Mostra o backend opcional Herdr e o fallback local",
    "/workspace": "Mostra o workspace base de escrita/execução",
    "/save": "Salva resumo da sessão em historico/",
    "/clear": "Limpa o histórico da conversa",
    "/exit": "Sai do agente",
}
SLASH_ALIASES = {
    "/ajuda": "/help",
    "/quit": "/exit",
    "/sair": "/exit",
    "/q": "/exit",
    "/limpar": "/clear",
    "/salvar": "/save",
    "/subagent": "/spawn",
    "/subagente": "/spawn",
    "/default": "/chat",
    "/verbosidade": "/verbosity",
    "/verbose": "/verbosity",
}

SKIPPED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "node_modules",
}
def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def rich_output_enabled(file: Any | None = None) -> bool:
    return Console is not None and file is None and sys.stdout.isatty() and not os.getenv("NO_COLOR")


def color_output_enabled(file: Any | None = None) -> bool:
    stream = file or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and not os.getenv("NO_COLOR")


def inline_styled(text: str, style: str = "white", file: Any | None = None) -> str:
    ansi = ANSI_BY_STYLE.get(style, "") if color_output_enabled(file) else ""
    if not ansi:
        return text
    return f"{ansi}{text}{RESET}"


def get_console() -> Any | None:
    global _RICH_CONSOLE
    if not rich_output_enabled():
        return None
    if _RICH_CONSOLE is None:
        theme = Theme(RICH_THEME) if Theme is not None else None
        _RICH_CONSOLE = Console(theme=theme, highlight=False, soft_wrap=True)
    return _RICH_CONSOLE


def print_styled(text: str, style: str = "white", end: str = "\n", file: Any | None = None) -> None:
    console = get_console() if file is None else None
    if console is not None:
        console.print(text, style=RICH_STYLE_BY_NAME.get(style, style), end=end, markup=False, highlight=False)
        console.file.flush()
        return

    stream = file or sys.stdout
    ansi = ANSI_BY_STYLE.get(style, "") if color_output_enabled(stream) else ""
    reset = RESET if ansi else ""
    print(f"{ansi}{text}{reset}", end=end, file=file, flush=end == "")


def print_labeled(label: str, content: str = "", style: str = "cyan", content_style: str = "white") -> None:
    console = get_console()
    if console is not None and Text is not None:
        text = Text()
        text.append(label, style=RICH_STYLE_BY_NAME.get(style, style))
        if content:
            text.append(" ")
            text.append(content, style=RICH_STYLE_BY_NAME.get(content_style, content_style))
        console.print(text)
        return

    ansi = ANSI_BY_STYLE.get(style, "")
    ansi = ansi if color_output_enabled() else ""
    reset = RESET if ansi else ""
    content_ansi = ANSI_BY_STYLE.get(content_style, "") if content and color_output_enabled() else ""
    content_reset = RESET if content_ansi else ""
    suffix = f" {content_ansi}{content}{content_reset}" if content else ""
    print(f"{ansi}{label}{reset}{suffix}")


def configure_diagnostic_logging(workspace: Path) -> Path | None:
    global DIAGNOSTIC_LOG_PATH
    log_dir = (workspace / LOG_DIR_NAME).resolve()
    try:
        ensure_path_inside_workspace(workspace, log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{AGENT_SLUG}.log"
        handler = RotatingFileHandler(
            log_path,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        DIAGNOSTIC_LOG_PATH = None
        return None

    for current_handler in list(LOGGER.handlers):
        LOGGER.removeHandler(current_handler)
        current_handler.close()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    DIAGNOSTIC_LOG_PATH = log_path
    LOGGER.info("runtime_started agent=%s workspace=%s", AGENT_NAME, workspace)
    return log_path


AgentProfile = AgentManifest




class ApprovalUnavailableError(RuntimeError):
    """O runtime precisava de aprovação, mas o terminal não ofereceu entrada."""


@dataclass(frozen=True)
class HistorySaveResult:
    path: Path
    title: str
    transcript_digest: str
    fallback_reason: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CLI agentico para endpoint Huawei MaaS compatível com OpenAI."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv(BASE_URL_ENV, DEFAULT_BASE_URL),
        help=f"Endpoint OpenAI-compatible. Padrão: variável {BASE_URL_ENV} ou {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--model",
        default=os.getenv(MODEL_ENV),
        help=(
            f"Alias ou identificador real de modelo para chamar. "
            f"Padrão: variável {MODEL_ENV}; se ausente, usa --model-alias."
        ),
    )
    parser.add_argument(
        "--model-alias",
        default=os.getenv(MODEL_ALIAS_ENV, DEFAULT_MODEL_ALIAS),
        help=f"Alias de modelo a resolver. Padrão: variável {MODEL_ALIAS_ENV} ou {DEFAULT_MODEL_ALIAS}",
    )
    parser.add_argument(
        "--model-alias-file",
        default=os.getenv(MODEL_ALIAS_FILE_ENV, DEFAULT_MODEL_ALIAS_FILE),
        help=(
            f"JSON com aliases de modelo. O default vem da distribuição; caminhos explícitos usam o workspace. "
            f"Padrão: variável {MODEL_ALIAS_FILE_ENV} ou {DEFAULT_MODEL_ALIAS_FILE}"
        ),
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Diretório base para caminhos relativos, escrita e execução local. Padrão: diretório atual.",
    )
    parser.add_argument(
        "--api-key-file",
        default=os.getenv(API_KEY_FILE_ENV, str(DEFAULT_API_KEY_FILE)),
        help=(
            f"Arquivo .txt da API key. Caminhos relativos são resolvidos a partir do perfil do usuário. "
            f"Padrão: variável {API_KEY_FILE_ENV} ou ~/cred/AgentA.txt"
        ),
    )
    parser.add_argument(
        "--agents-file",
        default="AGENTS.md",
        help="Arquivo de contexto. Usa AGENTS.md do workspace quando existir; senão, o da distribuição.",
    )
    parser.add_argument(
        "--skills-dir",
        default="skills",
        help="Diretório de Skills. Usa skills do workspace quando existir; senão, as da distribuição.",
    )
    parser.add_argument(
        "--profiles-dir",
        default=os.getenv(PROFILES_DIR_ENV, DEFAULT_PROFILES_DIR),
        help=(
            "Diretório com manifests TOML dos subagentes. O default vem da distribuição; "
            f"caminhos explícitos usam o workspace. Padrão: {PROFILES_DIR_ENV} ou {DEFAULT_PROFILES_DIR}"
        ),
    )
    parser.add_argument(
        "--user-profile",
        choices=("global", "grc"),
        default=None,
        help=(
            "Seleciona global ou grc somente na primeira execução não interativa. "
            "Depois, a escolha persistida em APPDATA é reutilizada."
        ),
    )
    parser.add_argument(
        "--no-project-context",
        action="store_true",
        help="Não carrega AGENTS.md, skills locais nem históricos salvos no prompt do agente.",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=os.getenv(HISTORY_LIMIT_ENV, str(DEFAULT_HISTORY_FILES)),
        help=(
            f"Quantidade de resumos recentes carregados de historico/. "
            f"Padrão: variável {HISTORY_LIMIT_ENV} ou {DEFAULT_HISTORY_FILES}"
        ),
    )
    parser.add_argument(
        "--read-scope",
        choices=sorted(READ_SCOPES),
        default=os.getenv(READ_SCOPE_ENV, "system"),
        help=(
            "Escopo para list_dir, read_file e search_text: system permite caminhos absolutos fora do workspace; "
            f"workspace preserva o limite antigo. Padrão: variável {READ_SCOPE_ENV} ou system."
        ),
    )
    parser.add_argument(
        "--write-scope",
        choices=sorted(WRITE_SCOPES),
        default=os.getenv(WRITE_SCOPE_ENV, "system"),
        help=(
            "Escopo para write_file: system permite caminhos absolutos e referências fora do workspace conforme /mode; "
            f"workspace preserva o limite antigo. Padrão: variável {WRITE_SCOPE_ENV} ou system."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Temperatura das respostas. Padrão: 0.1",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=DEFAULT_API_TIMEOUT_SECONDS,
        help=f"Timeout de cada chamada ao MaaS, em segundos. Padrão: {DEFAULT_API_TIMEOUT_SECONDS:g}",
    )
    parser.add_argument(
        "--api-retries",
        type=int,
        default=DEFAULT_API_RETRIES,
        help=f"Retries adicionais para falhas transitórias da API. Padrão: {DEFAULT_API_RETRIES}",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=(
            f"Teto de segurança de ciclos modelo->ferramenta. O orçamento cresce automaticamente em blocos "
            f"de {STEP_BUDGET_INCREMENT}. Padrão: {DEFAULT_MAX_STEPS}"
        ),
    )
    parser.add_argument(
        "--max-subagents",
        type=int,
        default=DEFAULT_MAX_SUBAGENTS,
        help=f"Máximo de subagentes que o orquestrador pode acionar por pedido. Padrão: {DEFAULT_MAX_SUBAGENTS}",
    )
    parser.add_argument(
        "--subagent-max-steps",
        type=int,
        default=DEFAULT_SUBAGENT_MAX_STEPS,
        help=f"Máximo de ciclos modelo->ferramenta por subagente. Padrão: {DEFAULT_SUBAGENT_MAX_STEPS}",
    )
    parser.add_argument(
        "--subagent-timeout",
        type=int,
        default=DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
        help=(
            "Timeout total de cada tarefa de subagente, separado do timeout de uma chamada MaaS. "
            f"Padrão: {DEFAULT_SUBAGENT_TIMEOUT_SECONDS} segundos"
        ),
    )
    parser.add_argument(
        "--permission-mode",
        choices=sorted(PERMISSION_MODES),
        default=os.getenv(PERMISSION_MODE_ENV, "strict"),
        help=(
            "Modo de aprovação local: strict, balanced ou auto. "
            f"Padrão: variável {PERMISSION_MODE_ENV} ou strict."
        ),
    )
    parser.add_argument(
        "--verbosity",
        default=os.getenv(VERBOSITY_MODE_ENV, "normal"),
        help=(
            "Modo de verbosidade das respostas: direto, normal ou detalhado. "
            f"Padrão: variável {VERBOSITY_MODE_ENV} ou normal."
        ),
    )
    parser.add_argument(
        "--no-shell",
        action="store_true",
        help="Desativa as ferramentas run_cli e run_powershell.",
    )
    parser.add_argument(
        "--allow-sensitive-read",
        action="store_true",
        help=(
            "Permite leitura de arquivos com nomes sensíveis sem prompt em qualquer modo. "
            "Sem esta flag, strict/balanced pedem aprovação e auto libera sem perguntar."
        ),
    )
    return parser.parse_args()


def resolve_user_file_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    return path.resolve()


def resolve_workspace_path(workspace: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def ensure_path_inside_workspace(workspace: Path, path: Path) -> None:
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError(f"Caminho de contexto fora do workspace permitido: {path}") from exc


def is_path_inside_workspace(workspace: Path, path: Path) -> bool:
    try:
        path.relative_to(workspace)
    except ValueError:
        return False
    return True


def load_agent_profiles(config: AgentConfig) -> dict[str, AgentProfile]:
    profiles_dir = config.profiles_dir.resolve()
    if not profiles_dir.exists():
        return {}
    if not profiles_dir.is_dir():
        raise NotADirectoryError(f"Diretório de personalidades inválido: {profiles_dir}")
    profile_paths = sorted(profiles_dir.glob("*.toml"), key=lambda path: path.name.lower())
    if len(profile_paths) > MAX_PROFILE_COUNT:
        raise ValueError(f"Máximo de {MAX_PROFILE_COUNT} perfis TOML permitido em {profiles_dir}")
    try:
        registry = AgentRegistry.load(
            profiles_dir,
            boundary=profiles_dir.parent,
            max_manifests=MAX_PROFILE_COUNT,
            max_manifest_bytes=MAX_PROFILE_FILE_BYTES,
        )
    except ManifestError as exc:
        raise ValueError(f"Campos não permitidos ou manifest de agente inválido: {exc}") from exc

    total_instructions_chars = sum(len(manifest.instructions) for manifest in registry.manifests)
    if total_instructions_chars > MAX_PROFILE_TOTAL_INSTRUCTIONS_CHARS:
        raise ValueError(
            f"Instruções dos manifests excedem {MAX_PROFILE_TOTAL_INSTRUCTIONS_CHARS} caracteres no total."
        )
    return {manifest.id: manifest for manifest in registry.manifests}


def select_agent_profile(
    profiles: dict[str, AgentProfile],
    requested_profile: str,
    task: str,
    required_capabilities: list[str] | tuple[str, ...] | None = None,
) -> AgentProfile | None:
    requested = requested_profile.strip().lower()
    if not profiles:
        if requested:
            raise ValueError(f"Perfil solicitado, mas nenhum TOML foi carregado: {requested}")
        return None
    if requested:
        if requested not in profiles:
            available = ", ".join(sorted(profiles))
            raise ValueError(f"Perfil de subagente desconhecido: {requested}. Disponíveis: {available}")
        selected = profiles[requested]
        if required_capabilities and not CapabilityMatcher.matches_all(selected, required_capabilities):
            raise ValueError(
                f"Perfil {requested} não possui todas as capabilities requeridas: "
                f"{', '.join(required_capabilities)}"
            )
        return selected

    if required_capabilities:
        matches = AgentRegistry(profiles.values()).find(required_capabilities)
        if not matches:
            raise ValueError(
                "Nenhum agente cobre as capabilities requeridas: " + ", ".join(required_capabilities)
            )
        return matches[0]

    normalized_task = normalize_reference_text(task)
    routes = (
        ("longato", ("pipeline", "github actions", "cicd", "ci cd", "deploy")),
        ("bond", ("iam", "permissao", "acesso", "identidade", "role", "privilegio", "zero trust")),
        ("baitz", ("readme", "documentacao", "runbook", "manual", "guia", "handoff")),
        ("anaconda", ("python", "sdk", "api", "csv", "json", "inventario")),
        ("capitao-kowalski", ("bash", "shell", "gcloud", "aws cli", "azure cli", "hcloud")),
    )
    for identifier, keywords in routes:
        if identifier in profiles and any(keyword in normalized_task for keyword in keywords):
            return profiles[identifier]
    if "bulk-worker" in profiles:
        return profiles["bulk-worker"]
    return profiles[sorted(profiles)[0]]


def format_agent_profile_catalog(profiles: dict[str, AgentProfile]) -> str:
    if not profiles:
        return "Nenhuma personalidade TOML foi carregada; use o perfil interno genérico."
    return "\n".join(
        f"- {identifier}: {profile.description} | capabilities={','.join(profile.capabilities) or 'nenhuma'} "
        f"| mutation_default=false | reasoning={profile.preferred_thinking} "
        f"| deep_thinking={'on' if profile.thinking_enabled else 'off'}"
        for identifier, profile in sorted(profiles.items())
    )


def normalize_reference_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()
    previous = None
    while previous != ascii_text:
        previous = ascii_text
        ascii_text = re.sub(r"^(na|no|em|a|o|minha|meu|pasta|diretorio|diretorio da|diretorio do)\s+", "", ascii_text)
    return ascii_text


PATH_REFERENCE_ALIASES = {
    "workspace": "workspace",
    "workdir": "workspace",
    "projeto": "workspace",
    "repositorio": "workspace",
    "pasta atual": "workspace",
    "diretorio atual": "workspace",
    "home": "home",
    "usuario": "home",
    "perfil": "home",
    "perfil do usuario": "home",
    "pasta do usuario": "home",
    "desktop": "desktop",
    "area de trabalho": "desktop",
    "ambiente de trabalho": "desktop",
    "downloads": "downloads",
    "download": "downloads",
    "baixados": "downloads",
    "documentos": "documents",
    "documents": "documents",
    "meus documentos": "documents",
    "onedrive": "onedrive",
    "one drive": "onedrive",
    "pictures": "pictures",
    "imagens": "pictures",
    "fotos": "pictures",
    "music": "music",
    "musicas": "music",
    "videos": "videos",
    "temp": "temp",
    "tmp": "temp",
    "temporario": "temp",
    "temporarios": "temp",
}


def normalize_path_reference(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_reference_text(value)
    if not normalized:
        return None
    if normalized in PATH_REFERENCE_VALUES:
        return normalized
    return PATH_REFERENCE_ALIASES.get(normalized)


def onedrive_candidates() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        value = os.getenv(env_name)
        if value:
            candidates.append(Path(value).expanduser())

    try:
        candidates.extend(path for path in Path.home().glob("OneDrive*") if path.is_dir())
    except OSError:
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def first_existing_path(candidates: list[Path]) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def resolve_known_folder(reference: str, workspace: Path) -> Path:
    home = Path.home()
    onedrives = onedrive_candidates()
    if reference == "workspace":
        return workspace
    if reference == "home":
        return home
    if reference == "onedrive":
        if onedrives:
            return first_existing_path(onedrives)
        return home / "OneDrive"
    if reference == "desktop":
        candidates = [drive / "Área de Trabalho" for drive in onedrives]
        candidates.extend(drive / "Desktop" for drive in onedrives)
        candidates.extend([home / "Área de Trabalho", home / "Desktop"])
        return first_existing_path(candidates)
    if reference == "downloads":
        return first_existing_path([home / "Downloads", home / "Transferências"])
    if reference == "documents":
        candidates = [drive / "Documentos" for drive in onedrives]
        candidates.extend(drive / "Documents" for drive in onedrives)
        candidates.extend([home / "Documentos", home / "Documents"])
        return first_existing_path(candidates)
    if reference == "pictures":
        return first_existing_path([home / "Imagens", home / "Pictures"])
    if reference == "music":
        return first_existing_path([home / "Músicas", home / "Music"])
    if reference == "videos":
        return first_existing_path([home / "Vídeos", home / "Videos"])
    if reference == "temp":
        return Path(tempfile.gettempdir())
    raise ValueError(f"Referência de caminho inválida: {reference}")


def normalize_api_key_candidate(raw_candidate: str) -> str:
    candidate = raw_candidate.strip().strip(";").strip().strip("\"'")
    candidate = re.sub(r"^export\s+", "", candidate, flags=re.IGNORECASE).strip()

    if ":" in candidate:
        header_name, header_value = candidate.split(":", 1)
        if header_name.strip().lower() == "authorization":
            candidate = header_value.strip().strip("\"'")

    if "=" in candidate:
        possible_name, possible_value = candidate.split("=", 1)
        normalized_name = possible_name.strip().lower()
        normalized_name = normalized_name.removeprefix("$env:").strip()
        if normalized_name in API_KEY_ASSIGNMENT_NAMES:
            candidate = possible_value.strip().strip(";").strip().strip("\"'")

    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip().strip("\"'")

    if not candidate:
        raise ValueError("API key vazia após normalização.")
    if any(ord(ch) < 32 for ch in candidate):
        raise ValueError("API key contém caracteres de controle.")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("API key contém espaços; deixe apenas a chave ou um formato NOME=VALOR suportado.")

    return candidate


def read_api_key_from_file(api_key_file: Path) -> str | None:
    if not api_key_file.exists():
        return None
    if not api_key_file.is_file():
        raise ValueError(f"O caminho da API key não é um arquivo: {api_key_file}")
    if api_key_file.stat().st_size > MAX_API_KEY_FILE_BYTES:
        raise ValueError(f"Arquivo de API key maior que {MAX_API_KEY_FILE_BYTES} bytes: {api_key_file}")

    content = api_key_file.read_text(encoding="utf-8-sig")
    for line in content.splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return normalize_api_key_candidate(candidate)

    raise ValueError(f"Arquivo de API key está vazio ou só contém comentários: {api_key_file}")


def read_api_key(api_key_file: Path) -> str:
    api_key = read_api_key_from_file(api_key_file)
    if api_key:
        return api_key

    api_key = os.getenv(API_KEY_ENV)
    if api_key:
        return normalize_api_key_candidate(api_key)

    api_key = getpass(f"{API_KEY_ENV} ou {api_key_file}: ").strip()
    if not api_key:
        raise ValueError(f"Crie {api_key_file}, defina {API_KEY_ENV} ou informe a chave no prompt.")
    return normalize_api_key_candidate(api_key)


def build_client(
    api_key: str,
    base_url: str,
    timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS,
    model: str = DEFAULT_MODEL,
) -> HuaweiMaaSAdapter:
    """Factory compatível que agora entrega o adapter assíncrono do MaaS."""
    provider_config = HuaweiMaaSConfig(
        api_key=SecretStr(api_key),
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=0,
    )
    capabilities = load_capabilities_snapshot(
        DEFAULT_CAPABILITIES_FILE,
        model=model,
        base_url=provider_config.base_url,
    )
    required = ("simple_chat", "tools", "reasoning_none", "reasoning_max", "no_openai_network")
    unavailable = [name for name in required if not capabilities.supports(name)]
    if unavailable:
        raise ValueError(
            "Modelo/endpoint sem capability probe compatível para o runtime: "
            + ", ".join(unavailable)
            + ". Execute e registre um novo probe antes de iniciar o agente."
        )
    return HuaweiMaaSAdapter(provider_config, capabilities=capabilities)


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def prepare_messages_for_api(messages: list[Message]) -> tuple[list[Message], int]:
    """Compatibilidade da política antiga; o runtime usa ContextBudget por tokens."""

    return prepare_messages_by_chars(messages, max_chars=MAX_API_MESSAGE_CHARS)


def sync_context_state_message(messages: list[Message], context_engine: ContextEngine) -> None:
    """Keep one replaceable structured-state anchor in the canonical history."""

    state_message: Message = {"role": "system", "content": context_engine.session_prompt()}
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "system" and str(message.get("content") or "").startswith("<session_state"):
            messages[index] = state_message
            return
    messages.insert(1 if messages else 0, state_message)


async def create_chat_completion_with_retry(
    client: ModelAdapter,
    *,
    operation: str,
    api_retries: int,
    emit_status: bool,
    loading_enabled: bool | None = None,
    loading_message: str = "Processando",
    request_timeout: float | None = None,
    request: ModelRequest,
    event_bus: EventBus | None = None,
    agent_name: str = AGENT_NAME,
    visible: bool = False,
    use_stream: bool = False,
    context_budget: ContextBudget | None = None,
) -> ModelResponse:
    active_budget = context_budget or ContextBudget(getattr(client, "capabilities", None))
    prepared = prepare_token_bounded_messages(
        request.messages,
        active_budget,
        tools=request.tools,
    )
    request = replace(
        request,
        messages=prepared.messages,
        max_tokens=request.max_tokens or active_budget.output_reserve_tokens,
    )
    attempts = max(1, api_retries + 1)
    show_loading = emit_status if loading_enabled is None else loading_enabled
    for attempt in range(1, attempts + 1):
        request_options = request
        if request_timeout is not None:
            request_options = replace(request, timeout_seconds=request_timeout)
        started_at = time.monotonic()
        emitted_stream_data = False
        if event_bus is not None:
            await event_bus.emit(
                "llm.request_started",
                source=agent_name,
                payload={
                    "agent": agent_name,
                    "model": client.model,
                    "attempt": attempt,
                    "visible": visible,
                },
            )
        try:
            if use_stream:
                response: ModelResponse | None = None
                legacy_text: list[str] = []
                first_token_emitted = False
                normalized_event_seen = False
                completed_event_seen = False
                async for stream_event in client.stream(request_options):
                    if isinstance(stream_event, str):
                        legacy_text.append(stream_event)
                        event_type = "text_delta"
                        event_text = stream_event
                        event_tool_call = None
                    else:
                        normalized_event_seen = True
                        event_type = stream_event.type
                        event_text = stream_event.text
                        event_tool_call = stream_event.tool_call

                    if event_type in {"first_token", "text_delta", "tool_call_started"}:
                        emitted_stream_data = True
                    if not first_token_emitted and event_type in {"first_token", "text_delta", "tool_call_started"}:
                        if event_type == "first_token" and not isinstance(stream_event, str):
                            latency = (stream_event.first_token_latency_ms or 0) / 1000
                        else:
                            latency = time.monotonic() - started_at
                        if event_bus is not None:
                            await event_bus.emit(
                                "llm.first_token",
                                source=agent_name,
                                payload={
                                    "agent": agent_name,
                                    "model": client.model,
                                    "first_token_latency_seconds": latency,
                                    "visible": visible,
                                },
                            )
                        first_token_emitted = True

                    if event_type == "text_delta" and event_text and event_bus is not None:
                        await event_bus.emit(
                            "llm.text_delta",
                            source=agent_name,
                            payload={"agent": agent_name, "text": event_text, "visible": visible},
                        )
                    elif event_type in {"tool_call_started", "tool_call_completed"} and event_tool_call:
                        if event_bus is not None:
                            await event_bus.emit(
                                f"llm.{event_type}",
                                source=agent_name,
                                payload={
                                    "agent": agent_name,
                                    "tool_call_id": event_tool_call.id,
                                    "tool": event_tool_call.name,
                                    "visible": visible,
                                },
                            )
                    elif event_type == "completed" and not isinstance(stream_event, str):
                        completed_event_seen = True
                        response = stream_event.response

                if response is None:
                    if normalized_event_seen:
                        detail = (
                            "sem ModelResponse"
                            if completed_event_seen
                            else "sem evento completed"
                        )
                        raise RuntimeError(f"Stream do modelo terminou {detail}.")
                    response = ModelResponse(content="".join(legacy_text))
            else:
                with LoadingIndicator(message=loading_message, enabled=show_loading):
                    response = await client.complete(request_options)
                if event_bus is not None and response.content:
                    await event_bus.emit(
                        "llm.first_token",
                        source=agent_name,
                        payload={
                            "agent": agent_name,
                            "model": client.model,
                            "first_token_latency_seconds": time.monotonic() - started_at,
                            "visible": visible,
                        },
                    )
                    await event_bus.emit(
                        "llm.text_delta",
                        source=agent_name,
                        payload={"agent": agent_name, "text": response.content, "visible": visible},
                    )

            budget_snapshot = active_budget.reconcile_usage(response.usage)
            if event_bus is not None:
                await event_bus.emit(
                    "llm.request_completed",
                    source=agent_name,
                    payload={
                        "agent": agent_name,
                        "model": client.model,
                        "attempt": attempt,
                        "status": "completed",
                        "duration_seconds": time.monotonic() - started_at,
                        "finish_reason": response.finish_reason,
                        "total_tokens": response.usage.get("total_tokens"),
                        "prompt_tokens": response.usage.get("prompt_tokens"),
                        "completion_tokens": response.usage.get("completion_tokens"),
                        "estimated_prompt_tokens": budget_snapshot.estimated_input_tokens,
                        "context_capacity_tokens": budget_snapshot.max_input_tokens,
                        "output_reserve_tokens": budget_snapshot.output_reserve_tokens,
                        "context_utilization": (
                            budget_snapshot.reconciled_input_tokens / budget_snapshot.max_input_tokens
                        ),
                        "visible": visible,
                    },
                )
            return response
        except OpenAIError as exc:
            retryable = is_retryable_api_error(exc)
            summary = exception_chain_summary(exc)
            LOGGER.warning(
                "api_call_failed operation=%s attempt=%s/%s retryable=%s error=%s",
                operation,
                attempt,
                attempts,
                retryable,
                summary,
            )
            if event_bus is not None:
                await event_bus.emit(
                    "llm.request_completed",
                    source=agent_name,
                    payload={
                        "agent": agent_name,
                        "model": client.model,
                        "attempt": attempt,
                        "status": "failed",
                        "duration_seconds": time.monotonic() - started_at,
                        "visible": visible,
                    },
                )
            if emitted_stream_data or not retryable or attempt >= attempts:
                raise
            delay_seconds = retry_delay_seconds(attempt, cap_seconds=4.0)
            if emit_status:
                print_labeled(
                    "API>",
                    f"falha transitória; nova tentativa {attempt + 1}/{attempts} em {delay_seconds}s.",
                    style="yellow",
                    content_style="yellow",
                )
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            if event_bus is not None:
                await event_bus.emit(
                    "llm.request_completed",
                    source=agent_name,
                    payload={
                        "agent": agent_name,
                        "model": client.model,
                        "attempt": attempt,
                        "status": "cancelled",
                        "duration_seconds": time.monotonic() - started_at,
                        "visible": visible,
                    },
                )
            raise
        except Exception:
            if event_bus is not None:
                await event_bus.emit(
                    "llm.request_completed",
                    source=agent_name,
                    payload={
                        "agent": agent_name,
                        "model": client.model,
                        "attempt": attempt,
                        "status": "failed",
                        "duration_seconds": time.monotonic() - started_at,
                        "visible": visible,
                    },
                )
            raise
    raise RuntimeError("Fluxo de retry da API terminou sem resposta ou exceção.")


def report_api_error(exc: OpenAIError) -> None:
    detail = exception_chain_summary(exc)
    LOGGER.error("api_request_aborted error=%s", detail)
    if isinstance(exc, APITimeoutError):
        message = f"Timeout na API após as tentativas configuradas: {detail}"
    elif isinstance(exc, APIConnectionError):
        message = f"Falha de conexão com a API após as tentativas configuradas: {detail}"
    elif isinstance(exc, APIStatusError):
        message = f"API retornou HTTP {exc.status_code}: {detail}"
    else:
        message = f"Erro da SDK OpenAI-compatible: {detail}"
    print_styled(message, style="red", file=sys.stderr)
    if DIAGNOSTIC_LOG_PATH is not None:
        print_styled(f"Diagnóstico salvo em {DIAGNOSTIC_LOG_PATH}", style="gray", file=sys.stderr)


def append_api_failure_context(messages: list[Message], turn_start: int, exc: OpenAIError) -> None:
    tool_results = sum(1 for message in messages[turn_start:] if message.get("role") == "tool")
    messages.append(
        {
            "role": "system",
            "content": (
                "A chamada anterior ao modelo falhou após o tratamento interno. "
                f"Detalhe sanitizado: {exception_chain_summary(exc)}. "
                f"Há {tool_results} resultado(s) de ferramenta preservado(s) neste turno. "
                "Ao continuar, não repita ações já concluídas; reutilize os resultados existentes e tente um contorno seguro."
            ),
        }
    )


def message_content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return to_json(content)


def render_message_for_history(index: int, message: Message) -> str:
    role = str(message.get("role", "unknown"))
    name = str(message.get("name") or "").strip()
    heading = f"### {index}. {role}"
    if name:
        heading += f" ({name})"

    sections = [heading]
    content = message_content_as_text(message.get("content")).strip()
    if content:
        sections.append(redact_sensitive_text(truncate_text(content, limit=8000)))

    tool_calls = message.get("tool_calls")
    if tool_calls:
        tool_call_text = redact_sensitive_text(truncate_text(to_json(tool_calls), limit=6000))
        sections.append(f"Tool calls:\n```json\n{tool_call_text}\n```")

    return "\n\n".join(sections)


def build_history_transcript(messages: list[Message]) -> str:
    conversation = messages[1:] if messages and messages[0].get("role") == "system" else messages
    rendered = [
        render_message_for_history(index, message)
        for index, message in enumerate(conversation, start=1)
    ]
    transcript = "\n\n".join(piece for piece in rendered if piece.strip()).strip()
    if len(transcript) <= MAX_HISTORY_TRANSCRIPT_CHARS:
        return transcript

    head_limit = MAX_HISTORY_TRANSCRIPT_CHARS // 3
    tail_limit = MAX_HISTORY_TRANSCRIPT_CHARS - head_limit
    return (
        f"{transcript[:head_limit]}"
        "\n\n[Transcrição intermediária truncada por limite de contexto.]\n\n"
        f"{transcript[-tail_limit:]}"
    )


def history_transcript_digest(transcript: str) -> str:
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()


def has_meaningful_history(messages: list[Message]) -> bool:
    return any(
        message.get("role") in {"user", "assistant", "tool"}
        and (message_content_as_text(message.get("content")).strip() or message.get("tool_calls"))
        for message in messages
    )


def strip_markdown_code_fence(value: str) -> str:
    text = value.strip()
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_history_summary_response(raw_content: str, title_hint: str = "") -> tuple[str, str]:
    candidate = strip_markdown_code_fence(raw_content)
    parsed: Any | None = None
    parse_candidates = [candidate]
    object_match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
    if object_match:
        parse_candidates.append(object_match.group(0))

    for parse_candidate in parse_candidates:
        try:
            parsed = json.loads(parse_candidate)
            break
        except json.JSONDecodeError:
            continue

    if isinstance(parsed, dict):
        raw_title = parsed.get("title") or parsed.get("titulo") or title_hint or "Sessão AgenteGlobal"
        raw_summary = (
            parsed.get("summary_markdown")
            or parsed.get("resumo_markdown")
            or parsed.get("summary")
            or parsed.get("resumo")
            or ""
        )
        summary = raw_summary if isinstance(raw_summary, str) else to_json(raw_summary)
        return truncate_single_line(str(raw_title), limit=120), summary.strip()

    title = title_hint.strip() or "Sessão AgenteGlobal"
    return truncate_single_line(title, limit=120), candidate


def slugify_history_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    slug = slug[:80].rstrip("-")
    return slug or "sessao-agenteglobal"


def build_history_summary_prompt(transcript: str, title_hint: str, config: AgentConfig) -> str:
    title_instruction = (
        f"\nPreferência de título informada pelo operador: {title_hint.strip()}"
        if title_hint.strip()
        else ""
    )
    return f"""Gere uma memória persistente da sessão atual do AgenteGlobal.

Objetivo:
- Criar um resumo técnico que permita continuar o trabalho em uma próxima execução do CLI.
- Preservar decisões, arquivos alterados, comandos relevantes, validações, pendências e riscos.
- Não incluir segredos, tokens, senhas, chaves privadas, API keys ou valores sensíveis.

Responda somente com JSON válido, sem markdown fence, neste formato:
{{
  "title": "título curto em português para nomear o histórico",
  "summary_markdown": "# Título\\n\\n## Resumo\\n...\\n\\n## Decisões e Configurações\\n...\\n\\n## Arquivos e Comandos Relevantes\\n...\\n\\n## Validação\\n...\\n\\n## Pendências / Próxima Sessão\\n..."
}}

Contexto operacional:
- Workspace: {config.workspace}
- Modo de permissão: {config.permission_mode}
- Escopo de leitura: {config.read_scope}
- Escopo de escrita: {config.write_scope}
{title_instruction}

Transcrição sanitizada da sessão:

{transcript}
"""


async def request_history_summary(
    client: ModelAdapter,
    model: str,
    transcript: str,
    title_hint: str,
    config: AgentConfig,
) -> tuple[str, str]:
    summary_messages: list[Message] = [
        {
            "role": "system",
            "content": (
                "Você resume sessões técnicas para continuidade operacional. "
                "Seja sóbrio, objetivo e nunca preserve segredos."
            ),
        },
        {
            "role": "user",
            "content": build_history_summary_prompt(transcript, title_hint, config),
        },
    ]
    response = await create_chat_completion_with_retry(
        client,
        operation="history_summary",
        api_retries=0,
        emit_status=True,
        loading_message="Salvando histórico",
        request_timeout=min(config.api_timeout_seconds, HISTORY_SUMMARY_TIMEOUT_SECONDS),
        request=ModelRequest(
            messages=summary_messages,
            temperature=0.1,
            reasoning_mode=ReasoningMode.NORMAL,
        ),
    )

    raw_content = response.content
    if not raw_content:
        raise ValueError("A API retornou um resumo vazio.")
    title, summary = parse_history_summary_response(raw_content, title_hint=title_hint)
    if not summary.strip():
        raise ValueError("A API retornou um resumo vazio.")
    return title, summary


def build_fallback_history_summary(transcript: str, reason: str) -> str:
    safe_transcript = redact_sensitive_text(truncate_text(transcript, limit=MAX_HISTORY_SUMMARY_CHARS - 1200))
    safe_transcript = safe_transcript.replace("```", "` ` `")
    safe_reason = redact_sensitive_text(truncate_single_line(reason, limit=300))
    return f"""## Resumo

Não foi possível gerar o resumo pelo modelo. O histórico abaixo preserva a transcrição sanitizada da sessão para continuidade manual.

## Motivo

{safe_reason}

## Transcrição Sanitizada

```text
{safe_transcript}
```
"""


def build_history_file_content(
    title: str,
    summary_markdown: str,
    model: str,
    config: AgentConfig,
    messages_considered: int,
    save_reason: str,
) -> str:
    body = redact_sensitive_text(truncate_text(summary_markdown.strip(), limit=MAX_HISTORY_SUMMARY_CHARS))
    if body.startswith("#"):
        body = "\n".join(body.splitlines()[1:]).lstrip()
    saved_at = datetime.now().isoformat(timespec="seconds")
    return f"""# {title}

> Resumo salvo pelo AgenteGlobal em {saved_at}.

## Metadados

- Modelo: `{model}`
- Workspace: `{config.workspace}`
- Modo de permissão: `{config.permission_mode}`
- Escopo de leitura: `{config.read_scope}`
- Escopo de escrita: `{config.write_scope}`
- Mensagens consideradas: `{messages_considered}`
- Origem do salvamento: `{save_reason}`

---

{body}
"""


def unique_history_path(history_dir: Path, title: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    slug = slugify_history_title(title)
    candidate = history_dir / f"{timestamp}-{slug}.md"
    counter = 2
    while candidate.exists():
        candidate = history_dir / f"{timestamp}-{slug}-{counter}.md"
        counter += 1
    return candidate


def atomic_write_text(path: Path, content: str) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def save_conversation_history(
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    config: AgentConfig,
    title_hint: str = "",
    prefer_model_summary: bool = True,
    save_reason: str = "manual",
) -> HistorySaveResult:
    transcript = build_history_transcript(messages)
    if not transcript or not has_meaningful_history(messages):
        raise ValueError("Não há conversa útil para salvar nesta sessão.")
    transcript_digest = history_transcript_digest(transcript)

    fallback_reason: str | None = None
    if prefer_model_summary:
        try:
            title, summary = await request_history_summary(
                client=client,
                model=model,
                transcript=transcript,
                title_hint=title_hint,
                config=config,
            )
        except (APITimeoutError, APIConnectionError, APIStatusError, OpenAIError, ValueError) as exc:
            title = title_hint.strip() or f"Sessão {AGENT_NAME}"
            fallback_reason = exception_chain_summary(exc)
            summary = build_fallback_history_summary(transcript, fallback_reason)
    else:
        title = title_hint.strip() or "Sessão AgenteGlobal"
        fallback_reason = "API indisponível nesta sessão; resumo local utilizado sem nova tentativa."
        summary = build_fallback_history_summary(transcript, fallback_reason)

    final_title = truncate_single_line(title_hint.strip() or title, limit=120)
    history_dir = (config.workspace / HISTORY_DIR_NAME).resolve()
    ensure_path_inside_workspace(config.workspace, history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)

    history_path = unique_history_path(history_dir, final_title)
    content = build_history_file_content(
        title=final_title,
        summary_markdown=summary,
        model=model,
        config=config,
        messages_considered=max(0, len(messages) - 1),
        save_reason=save_reason,
    )
    atomic_write_text(history_path, content)
    LOGGER.info(
        "history_saved path=%s reason=%s model_summary=%s messages=%s",
        history_path,
        save_reason,
        fallback_reason is None,
        max(0, len(messages) - 1),
    )
    return HistorySaveResult(
        path=history_path,
        title=final_title,
        transcript_digest=transcript_digest,
        fallback_reason=fallback_reason,
    )


async def save_history_if_changed(
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    config: AgentConfig,
    last_saved_digest: str | None,
    save_reason: str,
    prefer_model_summary: bool,
) -> HistorySaveResult | None:
    if not has_meaningful_history(messages):
        return None
    transcript = build_history_transcript(messages)
    if not transcript or history_transcript_digest(transcript) == last_saved_digest:
        return None
    return await save_conversation_history(
        client=client,
        model=model,
        messages=messages,
        config=config,
        prefer_model_summary=prefer_model_summary,
        save_reason=save_reason,
    )


def format_path_request(path: str | None, path_reference: str | None = None) -> str:
    if path_reference:
        return f"{path_reference}/{path or '.'}"
    return path or "."


def confirm_action(title: str, detail: str, destructive: bool = False) -> bool:
    with _APPROVAL_PROMPT_LOCK:
        task_board = _TASK_BOARD_CONTEXT.get()
        if task_board is not None:
            task_board.pause_for_approval()
        try:
            print(f"{YELLOW}Aprovação necessária:{RESET} {title}")
            print(detail)
            try:
                if destructive:
                    answer = input(f"{RED}Aprovar ação destrutiva? [y/N]:{RESET} ").strip().lower()
                    return answer == "y"

                answer = input(f"{YELLOW}Aprovar? [y/N]:{RESET} ").strip().lower()
                return answer in {"y", "yes", "s", "sim"}
            except EOFError as exc:
                raise ApprovalUnavailableError(
                    "A aprovação não pôde ser lida neste terminal. Execute em um console interativo "
                    "ou altere conscientemente o /mode antes de repetir."
                ) from exc
        finally:
            if task_board is not None:
                task_board.resume_after_approval()


def should_confirm_action(permission_mode: str, action: str, unsafe: bool = False) -> bool:
    if permission_mode == "auto":
        return False
    if permission_mode == "balanced":
        return unsafe
    if permission_mode == "strict":
        return action in {"write_file", "run_powershell", "run_cli", "spawn_subagent_mutation"}
    raise ValueError(f"Modo de permissão inválido: {permission_mode}")


class WorkspaceTools:
    def __init__(
        self,
        config: AgentConfig,
        client: ModelAdapter | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        allow_write: bool = True,
        agent_profiles: dict[str, AgentProfile] | None = None,
        process_runner: ProcessRunner | None = None,
        event_bus: EventBus | None = None,
        agent_name: str = AGENT_NAME,
        sensitive_read_allowed: bool | None = None,
        allowed_paths: Sequence[str | Path] | None = None,
        network_allowed: bool | None = None,
        artifact_store: ArtifactStore | None = None,
        context_engine: ContextEngine | None = None,
        governance: RuntimeGovernance | None = None,
        cloud_scope: CloudScopeResolver | None = None,
        cloud_context: str | dict[str, Any] | None = None,
        post_write_callback: Callable[[Path], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.model = model
        self.temperature = temperature
        self.allow_write = allow_write
        self.agent_profiles = agent_profiles or {}
        self.agent_registry = AgentRegistry(self.agent_profiles.values())
        self.delegation_controller = DelegationAdmissionController(self.agent_registry)
        self.process_runner = process_runner or ProcessRunner()
        self.event_bus = event_bus
        self.agent_name = agent_name
        self.sensitive_read_allowed = sensitive_read_allowed
        # Compatibilidade com manifests antigos; não é um sandbox de rede.
        self.network_allowed = network_allowed
        self._artifact_store = artifact_store
        self._context_engine = context_engine
        self.governance = governance or RuntimeGovernance()
        self.cloud_scope = cloud_scope or CloudScopeResolver()
        self.cloud_context = cloud_context
        self.post_write_callback = post_write_callback
        if allowed_paths is None:
            self.allowed_paths = None
        else:
            resolved_allowed_paths = tuple((self.config.workspace / Path(item)).resolve() for item in allowed_paths)
            if any(
                not is_path_inside_workspace(self.config.workspace, path)
                for path in resolved_allowed_paths
            ):
                raise ValueError("allowed_paths precisa permanecer dentro do workspace.")
            self.allowed_paths = resolved_allowed_paths
        self.subagents_started = 0
        self._subagent_lock = threading.Lock()

    def extract_path_reference(self, raw_path: str) -> tuple[str | None, str]:
        if not raw_path:
            return None, "."

        expanded = os.path.expandvars(raw_path.strip().strip("\"'"))
        expanded_path = Path(expanded).expanduser()
        if expanded_path.is_absolute() or expanded.startswith("\\\\"):
            return None, expanded

        normalized = expanded.replace("\\", "/")
        first_segment, separator, remainder = normalized.partition("/")
        reference = normalize_path_reference(first_segment)
        if not reference:
            return None, expanded
        return reference, remainder if separator else "."

    def resolve_user_path(self, user_path: str | None, path_reference: str | None = None) -> Path:
        raw_path = "." if not user_path else user_path
        expanded = os.path.expandvars(raw_path.strip().strip("\"'"))
        requested = Path(expanded).expanduser()
        if requested.is_absolute() or expanded.startswith("\\\\"):
            return requested.resolve()

        reference = normalize_path_reference(path_reference)
        reference_tail = expanded
        if not reference:
            reference, reference_tail = self.extract_path_reference(expanded)

        if reference:
            base_path = resolve_known_folder(reference, self.config.workspace)
            requested = Path(reference_tail).expanduser()
            if not requested.is_absolute():
                requested = base_path / requested
        else:
            requested = self.config.workspace / requested

        return requested.resolve()

    def resolve_read_path(self, user_path: str | None, path_reference: str | None = None) -> Path:
        resolved = self.resolve_user_path(user_path, path_reference)
        if self.config.read_scope == "workspace" and not is_path_inside_workspace(self.config.workspace, resolved):
            raise PermissionError(f"Caminho fora do workspace permitido: {resolved}")

        self._ensure_manifest_path_allowed(resolved, "read")

        return resolved

    def resolve_write_path(self, user_path: str | None, path_reference: str | None = None) -> Path:
        resolved = self.resolve_user_path(user_path, path_reference)
        if self.config.write_scope == "workspace" and not is_path_inside_workspace(self.config.workspace, resolved):
            raise PermissionError(f"Escrita fora do workspace permitido: {resolved}")

        self._ensure_manifest_path_allowed(resolved, "write")

        return resolved

    def _ensure_manifest_path_allowed(self, path: Path, operation: str) -> None:
        if self.allowed_paths is None:
            return
        if any(is_path_inside_workspace(root, path) for root in self.allowed_paths):
            return
        allowed = [self.display_path(root) for root in self.allowed_paths]
        raise PermissionError(
            f"Manifest negou {operation} fora de permissions.allowed_paths: {path}. Permitidos: {allowed}"
        )

    def resolve_path(self, user_path: str | None) -> Path:
        requested = Path("." if not user_path else user_path)
        if not requested.is_absolute():
            requested = self.config.workspace / requested
        resolved = requested.resolve()
        if not is_path_inside_workspace(self.config.workspace, resolved):
            raise PermissionError(f"Caminho fora do workspace permitido: {resolved}")

        return resolved

    def display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.config.workspace))
        except ValueError:
            return str(path)

    @property
    def artifact_store(self) -> ArtifactStore:
        if self._artifact_store is None:
            self._artifact_store = ArtifactStore(self.config.workspace)
        return self._artifact_store

    @property
    def context_engine(self) -> ContextEngine:
        if self._context_engine is None:
            self._context_engine = ContextEngine(
                self.config.workspace,
                artifact_store=self.artifact_store,
            )
        return self._context_engine

    def ensure_read_allowed(self, path: Path, operation: str) -> None:
        if not is_sensitive_path(path):
            return
        if self.sensitive_read_allowed is False:
            raise PermissionError("Manifest do subagente negou leitura de caminho sensível.")
        if (
            self.sensitive_read_allowed is True
            or self.config.allow_sensitive_read
            or self.config.permission_mode == "auto"
        ):
            return

        detail = to_json(
            {
                "operation": operation,
                "path": str(path),
                "permission_mode": self.config.permission_mode,
                "reason": "nome de arquivo ou diretório sensível",
            }
        )
        if not confirm_action("read_sensitive_file", detail):
            raise PermissionError(
                "Leitura sensível negada pelo usuário. Use /mode auto ou --allow-sensitive-read apenas se for aceitável enviar esse conteúdo ao modelo."
            )

    def list_dir(self, path: str = ".", max_entries: int = 100, path_reference: str = "") -> str:
        directory = self.resolve_read_path(path, path_reference)
        self.ensure_read_allowed(directory, "list_dir")
        if not directory.exists():
            raise FileNotFoundError(f"Diretório não encontrado: {directory}")
        if not directory.is_dir():
            raise NotADirectoryError(f"Não é um diretório: {directory}")

        entries: list[dict[str, Any]] = []
        try:
            directory_entries = sorted(directory.iterdir(), key=lambda entry: entry.name.lower())
        except OSError as exc:
            raise PermissionError(f"Não foi possível listar o diretório: {directory}") from exc

        for index, item in enumerate(directory_entries):
            if index >= max_entries:
                break
            try:
                item_type = "dir" if item.is_dir() else "file"
                item_size = None if item.is_dir() else item.stat().st_size
            except OSError:
                item_type = "unknown"
                item_size = None
            entries.append(
                {
                    "name": item.name,
                    "path": self.display_path(item),
                    "type": item_type,
                    "size_bytes": item_size,
                    "sensitive_name": is_sensitive_path(item),
                }
            )

        return to_json(
            {
                "path": str(directory),
                "entries": entries,
                "truncated": len(directory_entries) > max_entries,
                "total_entries": len(directory_entries),
            }
        )

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200, path_reference: str = "") -> str:
        file_path = self.resolve_read_path(path, path_reference)
        self.ensure_read_allowed(file_path, "read_file")

        if not file_path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Não é um arquivo: {file_path}")
        if file_path.stat().st_size > MAX_READ_BYTES:
            ingested = self.context_engine.ingest_file(file_path, query=file_path.name, top_k=1)
            return ingested.to_prompt()

        content = file_path.read_text(encoding="utf-8", errors="replace")
        if "\x00" in content:
            raise ValueError("Arquivo parece binário; leitura textual bloqueada.")

        lines = content.splitlines()
        first_index = max(start_line, 1) - 1
        selected = lines[first_index : first_index + max_lines]
        numbered = [f"{first_index + idx + 1}: {line}" for idx, line in enumerate(selected)]

        return to_json(
            {
                "path": self.display_path(file_path),
                "start_line": first_index + 1,
                "returned_lines": len(selected),
                "total_lines": len(lines),
                "content": "\n".join(numbered),
            }
        )

    def read_artifact(
        self,
        artifact_id: str,
        offset: int = 0,
        max_bytes: int = MAX_ARTIFACT_TOOL_READ_BYTES,
    ) -> str:
        record = self.artifact_store.get_metadata(artifact_id)
        content = self.artifact_store.read_text(artifact_id, offset=offset, limit=max_bytes)
        returned_bytes = len(content.encode("utf-8"))
        return to_json(
            {
                "artifact_id": artifact_id,
                "offset": offset,
                "returned_bytes": returned_bytes,
                "has_more": offset + returned_bytes < record.size,
                "content": content,
                "metadata": record.model_dump(mode="json"),
            }
        )

    def retrieve_context(
        self,
        query: str,
        top_k: int = 4,
        artifact_id: str | None = None,
        strategy: str = "hybrid",
    ) -> str:
        response = self.context_engine.retrieve(
            query,
            top_k=top_k,
            artifact_id=artifact_id,
            strategy=RetrievalStrategy(strategy),
        )
        return to_json(
            {
                "query": response.query,
                "strategy_requested": response.requested_strategy.value,
                "strategy_used": response.strategy_used,
                "semantic_status": response.semantic_status,
                "semantic_error": response.semantic_error,
                "results": [
                    {
                        "chunk_id": hit.chunk.id,
                        "artifact_id": hit.chunk.artifact_id,
                        "score": hit.score,
                        "ordinal": hit.chunk.ordinal,
                        "metadata": hit.chunk.metadata,
                        "content": hit.chunk.text[:2_000],
                    }
                    for hit in response.hits
                ],
            }
        )

    def write_file(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        path_reference: str = "",
    ) -> str:
        if not self.allow_write:
            raise PermissionError("Ferramenta write_file desativada para este agente.")

        file_path = self.resolve_write_path(path, path_reference)
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > MAX_WRITE_BYTES:
            raise ValueError(f"Conteúdo maior que {MAX_WRITE_BYTES} bytes.")
        if file_path.exists() and not overwrite:
            raise FileExistsError("Arquivo já existe. Use overwrite=true se quiser substituir.")

        outside_workspace = not is_path_inside_workspace(self.config.workspace, file_path)
        detail = to_json(
            {
                "path": self.display_path(file_path),
                "absolute_path": str(file_path),
                "path_reference": path_reference,
                "write_scope": self.config.write_scope,
                "outside_workspace": outside_workspace,
                "overwrite": overwrite,
                "bytes_utf8": encoded_size,
                "permission_mode": self.config.permission_mode,
            }
        )
        unsafe = overwrite or file_path.exists() or is_sensitive_path(file_path) or outside_workspace
        if should_confirm_action(self.config.permission_mode, "write_file", unsafe=unsafe) and not confirm_action(
            "write_file",
            detail,
        ):
            raise PermissionError("Usuário negou a escrita do arquivo.")

        file_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = (
            self.governance.checkpoints.guarded_write_text(file_path, content, reason="write_file")
            if self.governance.checkpoints is not None
            else None
        )
        if checkpoint is None:
            file_path.write_text(content, encoding="utf-8")
        return to_json(
            {
                "status": "written",
                "path": self.display_path(file_path),
                "absolute_path": str(file_path),
                "outside_workspace": outside_workspace,
                "write_scope": self.config.write_scope,
                "checkpoint_id": checkpoint.checkpoint_id if checkpoint is not None else None,
            }
        )

    def iter_search_files(self, root: Path) -> Any:
        if root.is_file():
            yield root
            return

        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda entry: entry.name.lower())
            except OSError:
                continue

            for entry in entries:
                if entry.name in SKIPPED_DIRS:
                    continue
                try:
                    if entry.is_dir():
                        stack.append(entry)
                    elif entry.is_file():
                        yield entry
                except OSError:
                    continue

    def search_text(
        self,
        pattern: str,
        path: str = ".",
        max_matches: int = 50,
        max_scanned_files: int = DEFAULT_MAX_SEARCH_SCANNED_FILES,
        path_reference: str = "",
    ) -> str:
        root = self.resolve_read_path(path, path_reference)
        self.ensure_read_allowed(root, "search_text")
        if not root.exists():
            raise FileNotFoundError(f"Caminho não encontrado: {root}")
        if not root.is_file() and not root.is_dir():
            raise ValueError(f"Caminho não é arquivo nem diretório: {root}")
        root_is_sensitive = is_sensitive_path(root)
        regex = re.compile(pattern)
        matches: list[dict[str, Any]] = []
        scanned_files = 0
        skipped_files = 0
        sensitive_skipped_files = 0
        scan_limit = max(1, min(max_scanned_files, 20_000))

        for file_path in self.iter_search_files(root):
            if len(matches) >= max_matches or scanned_files + skipped_files >= scan_limit:
                break
            if any(part in SKIPPED_DIRS for part in file_path.parts):
                continue
            if is_sensitive_path(file_path) and not self.config.allow_sensitive_read:
                if self.sensitive_read_allowed is False:
                    skipped_files += 1
                    sensitive_skipped_files += 1
                    continue
                if self.config.permission_mode == "auto" or root_is_sensitive:
                    pass
                else:
                    skipped_files += 1
                    sensitive_skipped_files += 1
                    continue
            try:
                if file_path.stat().st_size > MAX_READ_BYTES:
                    skipped_files += 1
                    continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped_files += 1
                continue

            scanned_files += 1
            if "\x00" in text:
                skipped_files += 1
                continue

            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": self.display_path(file_path),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= max_matches:
                        break

        return to_json(
            {
                "pattern": pattern,
                "matches": matches,
                "scanned_files": scanned_files,
                "skipped_files": skipped_files,
                "sensitive_skipped_files": sensitive_skipped_files,
                "max_scanned_files": scan_limit,
                "truncated": len(matches) >= max_matches or scanned_files + skipped_files >= scan_limit,
            }
        )

    def _prepare_powershell_request(
        self,
        command: str,
        timeout_seconds: int,
    ) -> ProcessRequest:
        if not self.config.allow_shell:
            raise PermissionError("Ferramenta run_powershell desativada por --no-shell.")
        if is_cloud_command_mutating(command):
            raise PermissionError(
                "Mutações cloud via run_powershell foram bloqueadas; use run_cli com argumentos separados "
                "para validar projeto/conta e alvo explicitamente."
            )
        timeout = max(1, min(timeout_seconds, 120))
        destructive = is_destructive_command(command)
        unsafe = is_unsafe_command(command, cli_name="powershell")
        detail = to_json(
            {
                "command": command,
                "cwd": str(self.config.workspace),
                "timeout_seconds": timeout,
                "destructive": destructive,
                "unsafe": unsafe,
                "permission_mode": self.config.permission_mode,
            }
        )
        if should_confirm_action(
            self.config.permission_mode,
            "run_powershell",
            unsafe=unsafe,
        ) and not confirm_action("run_powershell", detail, destructive=destructive):
            raise PermissionError("Usuário negou a execução do comando.")

        return ProcessRequest(
            argv=["powershell.exe", "-NoProfile", "-Command", command],
            cwd=self.config.workspace,
            env=safe_subprocess_env(),
            timeout_seconds=timeout,
        )

    @staticmethod
    def _format_process_result(completed: Any, *, command: str | None = None) -> str:
        result: dict[str, Any] = {}
        if command is not None:
            result["command"] = command
        result.update(
            {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        return to_json(result)

    async def _run_process_stream(
        self,
        request: ProcessRequest,
        *,
        on_stdout: Any = None,
        on_stderr: Any = None,
    ) -> Any:
        run_stream = getattr(self.process_runner, "run_stream", None)
        if callable(run_stream):
            return await run_stream(request, on_stdout=on_stdout, on_stderr=on_stderr)
        return await asyncio.to_thread(self.process_runner.run, request)

    def run_powershell(self, command: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
        request = self._prepare_powershell_request(command, timeout_seconds)
        completed = self.process_runner.run(request)
        return self._format_process_result(completed)

    async def run_powershell_async(
        self,
        command: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        *,
        on_stdout: Any = None,
        on_stderr: Any = None,
    ) -> str:
        request = self._prepare_powershell_request(command, timeout_seconds)
        completed = await self._run_process_stream(
            request,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        return self._format_process_result(completed)

    def _prepare_cli_request(
        self,
        cli: str,
        args: list[str] | None,
        timeout_seconds: int,
        *,
        execution_context: str | dict[str, Any] | None = None,
    ) -> tuple[ProcessRequest, str]:
        if not self.config.allow_shell:
            raise PermissionError("Ferramenta run_cli desativada por --no-shell.")
        cli = cli.strip()
        if not cli:
            raise ValueError("Nome da CLI não pode ser vazio.")
        if any(separator in cli for separator in ("\\", "/", ":")):
            raise ValueError("Informe apenas o nome da CLI, sem caminho absoluto ou relativo.")

        resolved_cli = shutil.which(cli)
        if not resolved_cli:
            raise FileNotFoundError(f"CLI não encontrada no PATH: {cli}")

        raw_args = args or []
        if not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
            raise ValueError("args precisa ser uma lista de strings.")

        # Resolve cloud scope per invocation.  Local/non-cloud CLIs retain the
        # exact historical argv and approval behaviour.
        effective_context = execution_context if execution_context not in (None, "") else self.cloud_context
        cloud_command = self.cloud_scope.prepare(cli, raw_args, context=effective_context)
        effective_args = list(cloud_command.args)
        for warning in cloud_command.warnings:
            LOGGER.warning("cloud_scope_warning cli=%s provider=%s warning=%s", cli, cloud_command.provider, warning)

        command_parts = [resolved_cli, *effective_args]
        command_display = subprocess.list2cmdline([cli, *effective_args])
        timeout = max(1, min(timeout_seconds, 300))
        destructive = is_destructive_command(command_display)
        unsafe = is_unsafe_command(command_display, cli_name=cli)
        detail = to_json(
            {
                "command": command_display,
                "resolved_cli": resolved_cli,
                "cwd": str(self.config.workspace),
                "timeout_seconds": timeout,
                "destructive": destructive,
                "unsafe": unsafe,
                "permission_mode": self.config.permission_mode,
            }
        )
        if should_confirm_action(
            self.config.permission_mode,
            "run_cli",
            unsafe=unsafe,
        ) and not confirm_action("run_cli", detail, destructive=destructive):
            raise PermissionError("Usuário negou a execução da CLI.")

        return (
            ProcessRequest(
                argv=command_parts,
                cwd=self.config.workspace,
                env=safe_subprocess_env(),
                timeout_seconds=timeout,
            ),
            command_display,
        )

    def run_cli(
        self,
        cli: str,
        args: list[str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        *,
        execution_context: str | dict[str, Any] | None = None,
        cloud_context: str | dict[str, Any] | None = None,
    ) -> str:
        if execution_context is not None and cloud_context is not None:
            raise ValueError("Informe apenas execution_context ou cloud_context.")
        request, command_display = self._prepare_cli_request(
            cli,
            args,
            timeout_seconds,
            execution_context=execution_context if execution_context is not None else cloud_context,
        )
        completed = self.process_runner.run(request)
        return self._format_process_result(completed, command=command_display)

    async def run_cli_async(
        self,
        cli: str,
        args: list[str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        *,
        execution_context: str | dict[str, Any] | None = None,
        cloud_context: str | dict[str, Any] | None = None,
        on_stdout: Any = None,
        on_stderr: Any = None,
    ) -> str:
        if execution_context is not None and cloud_context is not None:
            raise ValueError("Informe apenas execution_context ou cloud_context.")
        request, command_display = self._prepare_cli_request(
            cli,
            args,
            timeout_seconds,
            execution_context=execution_context if execution_context is not None else cloud_context,
        )
        completed = await self._run_process_stream(
            request,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        return self._format_process_result(completed, command=command_display)

    async def spawn_subagent(
        self,
        task: str,
        name: str = "subagente",
        scope: str = "",
        max_steps: int | None = None,
        allow_mutation: bool = False,
        profile: str = "",
        required_capabilities: list[str] | None = None,
        task_id: str | None = None,
        acceptance_criteria: list[str] | None = None,
        *,
        runtime_mutation_grant: bool = False,
        task_spec_override: TaskSpec | None = None,
        execution_context: str = "",
        execution_scope: Any = None,
    ) -> str:
        if self.client is None or not self.model:
            raise PermissionError("Subagentes indisponíveis: cliente/modelo não foram configurados.")
        if self.config.max_subagents < 1:
            raise PermissionError("Subagentes desativados por --max-subagents 0.")

        if task_spec_override is not None:
            expected_mutation = not task_spec_override.read_only
            if allow_mutation != expected_mutation:
                raise ValueError("allow_mutation precisa corresponder a TaskSpec.read_only.")
            task = task_spec_override.objective
            scope = "; ".join(task_spec_override.scope)
            max_steps = task_spec_override.limits.max_steps
            required_capabilities = list(task_spec_override.required_capabilities)
            acceptance_criteria = list(task_spec_override.acceptance_criteria)

        task = task.strip()
        required_capabilities = required_capabilities or []
        acceptance_criteria = acceptance_criteria or []
        selected_profile = select_agent_profile(
            self.agent_profiles,
            profile,
            task,
            required_capabilities,
        )
        name = name.strip() or (selected_profile.name if selected_profile else "subagente")
        if name == "subagente" and selected_profile is not None:
            name = selected_profile.name
        scope = scope.strip()
        if not task:
            raise ValueError("A tarefa do subagente não pode ser vazia.")
        if len(task) > MAX_SUBAGENT_TASK_CHARS:
            raise ValueError(f"Tarefa do subagente maior que {MAX_SUBAGENT_TASK_CHARS} caracteres.")
        if allow_mutation:
            if not runtime_mutation_grant:
                raise PermissionError(
                    "Mutação de subagente exige grant do runtime originado por ação explícita do operador."
                )
            if selected_profile is not None and not selected_profile.permits_mutation(
                runtime_grant=runtime_mutation_grant
            ):
                raise PermissionError(f"O manifest {selected_profile.id} não permite grant de mutação.")
            detail = to_json(
                {
                    "name": name,
                    "scope": scope,
                    "task": task,
                    "profile": selected_profile.identifier if selected_profile else "generic",
                    "permission_mode": self.config.permission_mode,
                }
            )
            if should_confirm_action(
                self.config.permission_mode,
                "spawn_subagent_mutation",
                unsafe=True,
            ) and not confirm_action("spawn_subagent com mutação", detail):
                raise PermissionError("Usuário negou subagente com capacidade de mutação.")

        requested_steps = max_steps if max_steps is not None else self.config.subagent_max_steps
        manifest_steps = selected_profile.limits.max_steps if selected_profile else self.config.subagent_max_steps
        bounded_steps = max(1, min(int(requested_steps), self.config.subagent_max_steps, manifest_steps))
        requested_timeout = (
            task_spec_override.limits.timeout_seconds
            if task_spec_override is not None
            else self.config.subagent_timeout_seconds
        )
        manifest_timeout = selected_profile.limits.timeout_seconds if selected_profile else requested_timeout
        bounded_timeout = max(1, min(requested_timeout, manifest_timeout))
        max_output_chars = selected_profile.limits.max_output_chars if selected_profile else MAX_TOOL_OUTPUT_CHARS
        with self._subagent_lock:
            if self.subagents_started >= self.config.max_subagents:
                raise RuntimeError(f"Limite de {self.config.max_subagents} subagentes atingido neste pedido.")
            self.subagents_started += 1
            sequence = self.subagents_started

        resolved_task_id = task_id or f"TASK-SUB-{sequence:04d}"
        task_metadata = {
            **(task_spec_override.metadata if task_spec_override is not None else {}),
            "profile": selected_profile.identifier if selected_profile else "generic",
            "delegated_by": self.agent_name,
        }
        if task_spec_override is not None:
            task_spec = TaskSpec.model_validate(
                {
                    **task_spec_override.model_dump(mode="python"),
                    "task_id": resolved_task_id,
                    "limits": {
                        **task_spec_override.limits.model_dump(mode="python"),
                        "timeout_seconds": bounded_timeout,
                        "max_steps": bounded_steps,
                    },
                    "metadata": task_metadata,
                }
            )
        else:
            task_spec = TaskSpec(
                task_id=resolved_task_id,
                type="delegated_task",
                objective=task,
                scope=[scope] if scope else [],
                read_only=not allow_mutation,
                required_capabilities=required_capabilities,
                acceptance_criteria=acceptance_criteria,
                limits=TaskLimits(
                    timeout_seconds=bounded_timeout,
                    max_steps=bounded_steps,
                ),
                metadata=task_metadata,
            )
        await self.governance.before_agent_spawn(
            resolved_task_id,
            agent=name,
            allow_mutation=allow_mutation,
        )

        sub_config = replace(
            self.config,
            allow_shell=(
                self.config.allow_shell
                and allow_mutation
                and (selected_profile.permissions.allow_shell if selected_profile else True)
            ),
            allow_sensitive_read=(
                self.config.allow_sensitive_read
                and (selected_profile.permissions.allow_sensitive_read if selected_profile else False)
            ),
            max_steps=bounded_steps,
            api_timeout_seconds=self.config.api_timeout_seconds,
            max_subagents=0,
            subagent_max_steps=0,
        )
        sub_runner = WorkspaceTools(
            sub_config,
            client=self.client,
            model=self.model,
            temperature=self.temperature,
            allow_write=allow_mutation,
            agent_profiles=self.agent_profiles,
            event_bus=self.event_bus,
            agent_name=name,
            sensitive_read_allowed=(
                self.config.allow_sensitive_read
                and (selected_profile.permissions.allow_sensitive_read if selected_profile else False)
            ),
            allowed_paths=(
                selected_profile.permissions.allowed_paths or ["."]
                if selected_profile is not None
                else ["."]
            ),
            network_allowed=(selected_profile.permissions.allow_network if selected_profile else False),
            artifact_store=self.artifact_store,
            context_engine=self._context_engine,
            governance=self.governance,
            cloud_scope=self.cloud_scope,
            cloud_context=execution_context,
            post_write_callback=self.post_write_callback,
        )
        sub_tool_schemas = build_tool_schemas(
            allow_shell=sub_config.allow_shell,
            allow_write=allow_mutation,
            allow_subagents=False,
        )
        result_protocol = AgentResultFunctionProtocol(max_repairs=1)
        sub_tool_schemas.append(result_protocol.function_schema())
        sub_messages = create_subagent_messages(
            config=sub_config,
            name=name,
            task=task,
            scope=scope,
            allow_mutation=allow_mutation,
            profile=selected_profile,
            task_spec=task_spec,
        )
        task_context_slice = self.context_engine.context_slice(
            task_spec,
            policy=selected_profile.context if selected_profile is not None else None,
        )
        bounded_execution_context = execution_context.strip()
        if len(bounded_execution_context) > MAX_USER_INPUT_CHARS:
            bounded_execution_context = self.context_engine.ingest_text(
                bounded_execution_context,
                source_name=f"scheduler-context-{task_spec.task_id}.txt",
                query=task_spec.objective,
            ).to_prompt()
        isolated_context = (
            "Fatia de contexto isolada e vinculada à TaskSpec:\n"
            f"{json.dumps(task_context_slice.to_prompt_data(), ensure_ascii=False, indent=2)}"
        )
        if bounded_execution_context:
            isolated_context += (
                "\n\nContexto determinístico fornecido pelo DAG Scheduler para esta tentativa:\n"
                f"{bounded_execution_context}"
            )
        sub_messages.append({"role": "user", "content": isolated_context})
        effective_sub_runner = (
            execution_scope.child(sub_runner) if execution_scope is not None else sub_runner
        )
        try:
            async with asyncio.timeout(bounded_timeout):
                answer = await run_agent_until_final(
                    client=self.client,
                    model=self.model,
                    messages=sub_messages,
                    tools_runner=effective_sub_runner,
                    tool_schemas=sub_tool_schemas,
                    temperature=self.temperature,
                    max_steps=bounded_steps,
                    api_retries=sub_config.api_retries,
                    emit_tools=False,
                    event_bus=self.event_bus,
                    agent_name=name,
                    emit_run=False,
                    stream_output=False,
                    reasoning_mode=(selected_profile.reasoning_mode if selected_profile is not None else ReasoningMode.DEEP),
                    result_protocol=result_protocol,
                    result_task_id=resolved_task_id,
                )
        except TimeoutError:
            answer = AgentResult(
                task_id=resolved_task_id,
                status=AgentResultStatus.FAILED,
                summary="O subagente excedeu o timeout do contrato.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.TIMEOUT,
                        message=f"Execução excedeu {bounded_timeout} segundos.",
                    )
                ],
            ).model_dump_json()
        if len(answer) > max_output_chars:
            answer = AgentResult(
                task_id=resolved_task_id,
                status=AgentResultStatus.FAILED,
                summary="O resultado do subagente excedeu o limite do manifest.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.VALIDATION_ERROR,
                        message=f"AgentResult excedeu max_output_chars={max_output_chars}.",
                        details={"actual_chars": len(answer)},
                    )
                ],
            ).model_dump_json()
        try:
            agent_result = AgentResult.model_validate_json(answer)
        except ValidationError as exc:
            agent_result = AgentResult(
                task_id=resolved_task_id,
                status=AgentResultStatus.FAILED,
                summary="O subagente retornou um payload fora do contrato AgentResult.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.PROTOCOL_ERROR,
                        message="Resultado final inválido após a fronteira de Function Calling.",
                        details={"validation_error_count": len(exc.errors(include_input=False))},
                    )
                ],
            )

        await self.governance.after_agent_spawn(
            resolved_task_id,
            agent=name,
            status=agent_result.status.value,
            validated=agent_result.status is AgentResultStatus.COMPLETED,
        )
        return to_json(
            {
                "subagent": name,
                "status": agent_result.status.value,
                "model": self.model,
                "steps_limit": bounded_steps,
                "timeout_seconds": bounded_timeout,
                "max_output_chars": max_output_chars,
                "allow_mutation": allow_mutation,
                "profile": selected_profile.identifier if selected_profile else "generic",
                "answer": agent_result.summary,
                "agent_result": agent_result.model_dump(mode="json"),
                "task_spec": task_spec.model_dump(mode="json"),
            }
        )

    async def delegate_task(
        self,
        task_spec: TaskSpec | dict[str, Any],
        allow_mutation: bool = False,
        *,
        runtime_mutation_grant: bool = False,
        dependencies_resolved: bool = False,
        execution_context: str = "",
        execution_scope: Any = None,
    ) -> str:
        """Admission + capability routing acionável pelo manager sem depender de `/spawn`."""

        spec = task_spec if isinstance(task_spec, TaskSpec) else TaskSpec.model_validate(task_spec)
        delegated_limit_updates: dict[str, int] = {}
        if "max_steps" not in spec.limits.model_fields_set:
            delegated_limit_updates["max_steps"] = self.config.subagent_max_steps
        if "timeout_seconds" not in spec.limits.model_fields_set:
            delegated_limit_updates["timeout_seconds"] = self.config.subagent_timeout_seconds
        if delegated_limit_updates:
            spec = spec.model_copy(
                update={"limits": spec.limits.model_copy(update=delegated_limit_updates)}
            )
        if allow_mutation and not runtime_mutation_grant:
            raise PermissionError(
                "Mutação delegada exige grant do runtime originado por ação explícita do operador."
            )
        if allow_mutation and spec.read_only:
            raise ValueError("TaskSpec read_only=true não pode receber grant de mutação.")
        if not spec.read_only and not allow_mutation:
            raise PermissionError("TaskSpec read_only=false exige grant explícito de mutação do runtime.")
        decision = self.delegation_controller.decide(
            spec,
            dependencies_resolved=dependencies_resolved,
            runtime_mutation_grant=runtime_mutation_grant,
        )
        payload: dict[str, Any] = {"decision": decision.model_dump(mode="json"), "results": []}
        if decision.action is DelegationAction.EXECUTE_LOCALLY:
            return to_json(payload)

        remaining = max(0, self.config.max_subagents - self.subagents_started)
        selected_ids = list(decision.selected_agents[:remaining])
        if not selected_ids:
            payload["decision"]["action"] = DelegationAction.EXECUTE_LOCALLY.value
            payload["decision"]["reasons"] = [*payload["decision"]["reasons"], "subagent_limit_reached"]
            return to_json(payload)

        async def run_selected(agent_id: str) -> str:
            manifest = self.agent_registry.get(agent_id)
            agent_capabilities = (
                [cap for cap in decision.required_capabilities if manifest and cap in manifest.capabilities]
                or list(decision.required_capabilities)
            )
            if len(selected_ids) == 1:
                child_task_id = spec.task_id
            else:
                suffix = f":{agent_id}"
                child_task_id = f"{spec.task_id[: 128 - len(suffix)]}{suffix}"
            child_metadata = {
                **spec.metadata,
                "agent_focus_capabilities": agent_capabilities,
            }
            if len(selected_ids) > 1:
                child_metadata["parent_task_id"] = spec.task_id
            child_spec = TaskSpec.model_validate(
                {
                    **spec.model_dump(mode="python"),
                    "task_id": child_task_id,
                    "required_capabilities": agent_capabilities,
                    "metadata": child_metadata,
                }
            )
            return await self.spawn_subagent(
                task=spec.objective,
                scope="; ".join(spec.scope),
                max_steps=spec.limits.max_steps,
                allow_mutation=allow_mutation,
                profile=agent_id,
                required_capabilities=agent_capabilities,
                task_id=child_task_id,
                acceptance_criteria=spec.acceptance_criteria,
                runtime_mutation_grant=runtime_mutation_grant,
                task_spec_override=child_spec,
                execution_context=execution_context,
                execution_scope=execution_scope,
            )

        if allow_mutation or len(selected_ids) == 1:
            results = [await run_selected(agent_id) for agent_id in selected_ids]
        else:
            results = await asyncio.gather(*(run_selected(agent_id) for agent_id in selected_ids))
        payload["results"] = [json.loads(result) for result in results]
        return to_json(payload)

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            arguments = validate_tool_arguments(name, arguments)
            if name == "list_dir":
                return self.list_dir(**arguments)
            if name == "read_file":
                return self.read_file(**arguments)
            if name == "read_artifact":
                return self.read_artifact(**arguments)
            if name == "retrieve_context":
                return self.retrieve_context(**arguments)
            if name == "search_text":
                return self.search_text(**arguments)
            if name == "write_file":
                return self.write_file(**arguments)
            if name == "run_powershell":
                return self.run_powershell(**arguments)
            if name == "run_cli":
                return self.run_cli(**arguments)
            if name == "spawn_subagent":
                raise RuntimeError("spawn_subagent deve ser executado pela fronteira assíncrona.")
            if name == "delegate_task":
                raise RuntimeError("delegate_task deve ser executado pela fronteira assíncrona.")
            raise ValueError(f"Ferramenta desconhecida: {name}")
        except (ApprovalUnavailableError, KeyboardInterrupt):
            raise
        except Exception as exc:
            safe_error = redact_sensitive_text(truncate_single_line(str(exc), 1000))
            LOGGER.warning(
                "tool_failed tool=%s error=%s",
                name,
                f"{type(exc).__name__}: {safe_error}",
            )
            return to_json(
                {
                    "error": type(exc).__name__,
                    "message": safe_error,
                    "api_error": isinstance(exc, OpenAIError),
                }
            )

    async def _emit_tool_event(
        self,
        name: str,
        *,
        tool_id: str,
        payload: dict[str, Any],
    ) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.emit(
            name,
            source=self.agent_name,
            payload={
                "agent": self.agent_name,
                "tool_id": tool_id,
                **payload,
            },
        )

    @staticmethod
    def _tool_failed_result(result: str) -> tuple[bool, int | None]:
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return False, None
        if not isinstance(parsed, dict):
            return False, None
        returncode = parsed.get("returncode")
        failed = bool(parsed.get("error")) or (isinstance(returncode, int) and returncode != 0)
        return failed, returncode if isinstance(returncode, int) else None

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        runtime_mutation_grant: bool = False,
        dependencies_resolved: bool = False,
        execution_context: str = "",
        execution_scope: Any = None,
        policy_approval_granted: bool = False,
        policy_validation_passed: bool = False,
    ) -> str:
        """Executa tools sem bloquear o loop e publica observabilidade incremental."""
        started_at = time.monotonic()
        tool_id = f"{name}-{time.monotonic_ns()}"
        if name == "run_cli":
            cli = str(arguments.get("cli") or "CLI")
            activity = redact_sensitive_text(truncate_single_line(cli, 120))
        elif name == "run_powershell":
            activity = "PowerShell"
        elif name in {"spawn_subagent", "delegate_task"}:
            activity = str(arguments.get("name") or arguments.get("profile") or "subagent")
        else:
            activity = name
        await self._emit_tool_event(
            "tool.started",
            tool_id=tool_id,
            payload={"tool": name, "activity": activity},
        )

        output_buffers = {"tool.stdout": "", "tool.stderr": ""}

        async def emit_output(event_name: str, text: str) -> None:
            """Publica somente linhas completas e redigidas; a captura final permanece intacta."""
            combined = output_buffers[event_name] + text
            lines = combined.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                output_buffers[event_name] = lines.pop()
            else:
                output_buffers[event_name] = ""
            for line in lines:
                await self._emit_tool_event(
                    event_name,
                    tool_id=tool_id,
                    payload={"tool": name, "text": redact_sensitive_text(line)},
                )

        async def flush_output() -> None:
            for event_name, text in output_buffers.items():
                if text:
                    await self._emit_tool_event(
                        event_name,
                        tool_id=tool_id,
                        payload={"tool": name, "text": redact_sensitive_text(text)},
                    )
                    output_buffers[event_name] = ""

        try:
            arguments = validate_tool_arguments(name, arguments)
            if (
                not runtime_mutation_grant
                and name in {"spawn_subagent", "delegate_task"}
                and bool(arguments.get("allow_mutation"))
            ):
                # The model may request mutation, but only the operator-selected
                # permission mode can turn that request into a runtime grant.
                # Planning and approved-plan wrappers keep their own stricter
                # boundaries before reaching this point.
                runtime_mutation_grant = resolve_mutation_grant(
                    permission_mode=self.config.permission_mode,
                    allow_mutation=True,
                    operator_state_active=True,
                ).runtime_mutation_grant
            await self.governance.before_tool(
                name,
                arguments,
                actor=self.agent_name,
                correlation_id=tool_id,
                approval_granted=policy_approval_granted,
                validation_passed=policy_validation_passed,
            )
            if name == "run_cli":
                result = await self.run_cli_async(
                    **arguments,
                    execution_context=execution_context,
                    on_stdout=lambda text: emit_output("tool.stdout", text),
                    on_stderr=lambda text: emit_output("tool.stderr", text),
                )
            elif name == "run_powershell":
                result = await self.run_powershell_async(
                    **arguments,
                    on_stdout=lambda text: emit_output("tool.stdout", text),
                    on_stderr=lambda text: emit_output("tool.stderr", text),
                )
            elif name == "spawn_subagent":
                result = await self.spawn_subagent(
                    **arguments,
                    runtime_mutation_grant=runtime_mutation_grant,
                )
            elif name == "delegate_task":
                result = await self.delegate_task(
                    **arguments,
                    runtime_mutation_grant=runtime_mutation_grant,
                    dependencies_resolved=dependencies_resolved,
                    execution_context=execution_context,
                    execution_scope=execution_scope,
                )
            else:
                result = await asyncio.to_thread(self.execute, name, arguments)

            if name == "write_file" and self.post_write_callback is not None:
                try:
                    write_result = json.loads(result)
                    absolute_path = write_result.get("absolute_path") if isinstance(write_result, dict) else None
                    if isinstance(absolute_path, str) and absolute_path:
                        await self.post_write_callback(Path(absolute_path))
                except (OSError, ValueError, TypeError) as error:
                    await self._emit_tool_event(
                        "codeintel.write_sync_failed",
                        tool_id=tool_id,
                        payload={"tool": name, "error": type(error).__name__},
                    )

            await flush_output()
            failed, returncode = self._tool_failed_result(result)
            result, artifact = externalize_tool_result(
                self.artifact_store,
                name,
                result,
                max_inline_bytes=MAX_TOOL_OUTPUT_CHARS,
                redactor=redact_sensitive_text,
            )
            if self.event_bus is not None and name == "retrieve_context":
                try:
                    retrieval_payload = json.loads(result)
                    hit_count = len(retrieval_payload.get("results", ())) if isinstance(retrieval_payload, dict) else 0
                except (json.JSONDecodeError, TypeError):
                    hit_count = 0
                await self.event_bus.emit(
                    "retrieval.completed",
                    source=self.agent_name,
                    payload={"hit_count": hit_count, "status": "hit" if hit_count else "miss"},
                )
            if self.event_bus is not None and artifact is not None:
                await self.event_bus.emit(
                    "artifact.created",
                    source=self.agent_name,
                    payload={"status": "created", "size_bytes": artifact.size},
                )
            await self.governance.after_tool(
                name,
                arguments,
                validation_passed=not failed,
                correlation_id=tool_id,
            )
            await self._emit_tool_event(
                "tool.failed" if failed else "tool.completed",
                tool_id=tool_id,
                payload={
                    "tool": name,
                    "status": "failed" if failed else "completed",
                    "returncode": returncode,
                    "artifact_id": artifact.artifact_id if artifact is not None else None,
                    "size_bytes": artifact.size if artifact is not None else None,
                    "duration_seconds": time.monotonic() - started_at,
                },
            )
            return result
        except asyncio.CancelledError:
            await flush_output()
            await self._emit_tool_event(
                "tool.failed",
                tool_id=tool_id,
                payload={
                    "tool": name,
                    "status": "cancelled",
                    "duration_seconds": time.monotonic() - started_at,
                },
            )
            raise
        except (ApprovalUnavailableError, KeyboardInterrupt):
            await flush_output()
            await self._emit_tool_event(
                "tool.failed",
                tool_id=tool_id,
                payload={
                    "tool": name,
                    "status": "cancelled",
                    "duration_seconds": time.monotonic() - started_at,
                },
            )
            raise
        except Exception as exc:
            await flush_output()
            safe_error = redact_sensitive_text(truncate_single_line(str(exc), 1000))
            LOGGER.warning(
                "tool_failed tool=%s error=%s",
                name,
                f"{type(exc).__name__}: {safe_error}",
            )
            await self._emit_tool_event(
                "tool.failed",
                tool_id=tool_id,
                payload={
                    "tool": name,
                    "status": "failed",
                    "duration_seconds": time.monotonic() - started_at,
                },
            )
            return to_json(
                {
                    "error": type(exc).__name__,
                    "message": safe_error,
                    "api_error": isinstance(exc, OpenAIError),
                }
            )


def build_tool_schemas(
    allow_shell: bool,
    allow_write: bool = True,
    allow_subagents: bool = True,
    subagent_max_steps: int = DEFAULT_SUBAGENT_MAX_STEPS,
    profile_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "Lista arquivos e diretórios em caminho relativo ao workspace ou caminho absoluto permitido pelo escopo de leitura.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "path_reference": {
                            "type": "string",
                            "default": "",
                            "enum": ["", *PATH_REFERENCE_VALUES],
                            "description": "Base semântica opcional para path relativo. Use desktop para 'Área de Trabalho', downloads, documents, home, onedrive, temp etc.",
                        },
                        "max_entries": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Lê trechos de arquivo textual em caminho relativo ao workspace ou caminho absoluto permitido. Arquivos sensíveis podem exigir aprovação conforme /mode.",
                "parameters": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                        "path_reference": {
                            "type": "string",
                            "default": "",
                            "enum": ["", *PATH_REFERENCE_VALUES],
                            "description": "Base semântica opcional para path relativo. Use desktop para 'Área de Trabalho', downloads, documents, home, onedrive, temp etc.",
                        },
                        "start_line": {"type": "integer", "default": 1, "minimum": 1},
                        "max_lines": {"type": "integer", "default": 200, "minimum": 1, "maximum": 500},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_artifact",
                "description": "Lê uma fatia UTF-8 limitada de um artifact local recuperável pelo artifact_id.",
                "parameters": ReadArtifactArguments.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "retrieve_context",
                "description": "Recupera chunks locais relevantes por busca lexical/híbrida; sem backend semântico, informa o fallback explicitamente.",
                "parameters": RetrieveContextArguments.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_text",
                "description": "Busca texto ou regex em arquivos textuais em caminho relativo ao workspace ou caminho absoluto permitido. A busca tem limite de arquivos para evitar varreduras longas.",
                "parameters": {
                    "type": "object",
                    "required": ["pattern"],
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string", "default": "."},
                        "path_reference": {
                            "type": "string",
                            "default": "",
                            "enum": ["", *PATH_REFERENCE_VALUES],
                            "description": "Base semântica opcional para path relativo. Use desktop para 'Área de Trabalho', downloads, documents, home, onedrive, temp etc.",
                        },
                        "max_matches": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                        "max_scanned_files": {
                            "type": "integer",
                            "default": DEFAULT_MAX_SEARCH_SCANNED_FILES,
                            "minimum": 1,
                            "maximum": 20000,
                        },
                    },
                },
            },
        },
    ]

    if allow_write:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Cria ou substitui um arquivo textual UTF-8 em caminho relativo, absoluto ou referência como desktop/downloads/documents. Pode exigir aprovação conforme /mode e write_scope.",
                    "parameters": {
                        "type": "object",
                        "required": ["path", "content"],
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Caminho absoluto, relativo ao workspace ou relativo a path_reference. Ex.: relatorio.csv com path_reference=desktop.",
                            },
                            "path_reference": {
                                "type": "string",
                                "default": "",
                                "enum": ["", *PATH_REFERENCE_VALUES],
                                "description": "Base semântica opcional. Use desktop quando o usuário disser 'na minha Área de Trabalho'.",
                            },
                            "content": {"type": "string"},
                            "overwrite": {"type": "boolean", "default": False},
                        },
                    },
                },
            }
        )

    if allow_shell:
        tools.extend(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "run_cli",
                        "description": (
                            "Executa uma CLI instalada no PATH usando argumentos separados, sem shell. "
                            "Use preferencialmente para aws, az, gcloud, hcloud, kubectl, terraform, git, gh, docker, helm, python, npm e similares."
                        ),
                        "parameters": {
                            "type": "object",
                            "required": ["cli"],
                            "properties": {
                                "cli": {
                                    "type": "string",
                                    "description": "Nome do executável no PATH, sem caminho. Ex.: aws, az, gcloud, hcloud, kubectl, terraform.",
                                },
                                "args": {
                                    "type": "array",
                                    "default": [],
                                    "items": {"type": "string"},
                                    "description": "Argumentos individuais da CLI. Não monte uma string única.",
                                },
                                "timeout_seconds": {
                                    "type": "integer",
                                    "default": DEFAULT_TIMEOUT_SECONDS,
                                    "minimum": 1,
                                    "maximum": 300,
                                },
                            },
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_powershell",
                        "description": "Executa um comando PowerShell no workspace permitido. Use apenas quando precisar de pipeline, redirecionamento ou recursos específicos do shell.",
                        "parameters": {
                            "type": "object",
                            "required": ["command"],
                            "properties": {
                                "command": {"type": "string"},
                                "timeout_seconds": {
                                    "type": "integer",
                                    "default": DEFAULT_TIMEOUT_SECONDS,
                                    "minimum": 1,
                                    "maximum": 120,
                                },
                            },
                        },
                    },
                },
            ]
        )

    if allow_subagents:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "spawn_subagent",
                    "description": (
                        "Aciona um subagente MaaS com o mesmo modelo da sessão para uma tarefa independente. "
                        "Use para pesquisa, leitura, validação ou revisão paralelizável."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["task"],
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Tarefa objetiva e autocontida para o subagente executar.",
                            },
                            "name": {
                                "type": "string",
                                "default": "subagente",
                                "description": "Nome curto do subagente para rastreabilidade.",
                            },
                            "scope": {
                                "type": "string",
                                "default": "",
                                "description": "Arquivos, pastas ou limites específicos da tarefa.",
                            },
                            "max_steps": {
                                "type": "integer",
                                "default": subagent_max_steps,
                                "minimum": 1,
                                "maximum": subagent_max_steps,
                            },
                            "allow_mutation": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "Somente leitura por padrão. Defina true apenas quando o runtime tiver "
                                    "concedido mutação explicitamente e o /mode permitir."
                                ),
                            },
                            "required_capabilities": {
                                "type": "array",
                                "default": [],
                                "items": {"type": "string"},
                                "description": "Capabilities necessárias; têm precedência sobre o fallback por keywords.",
                            },
                            "task_id": {
                                "type": "string",
                                "description": "Identificador estável opcional da TaskSpec.",
                            },
                            "acceptance_criteria": {
                                "type": "array",
                                "default": [],
                                "items": {"type": "string"},
                            },
                            "profile": {
                                "type": "string",
                                "default": "",
                                **({"enum": ["", *profile_names]} if profile_names else {}),
                                "description": (
                                    "Personalidade TOML do subagente. Se omitida, o runtime escolhe "
                                    "automaticamente o perfil mais adequado à tarefa."
                                ),
                            },
                        },
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "description": (
                        "Submete uma TaskSpec ao Delegation Admission Controller. O runtime decide de forma "
                        "tipada entre executar localmente ou delegar por capabilities, sem exigir /spawn."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["task_spec"],
                        "properties": {
                            "task_spec": TaskSpec.model_json_schema(),
                            "allow_mutation": {
                                "type": "boolean",
                                "default": False,
                                "description": "Grant explícito do runtime; permanece false por padrão.",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            }
        )

    return tools


def build_planning_tool_schemas(
    *,
    allow_subagents: bool,
    subagent_max_steps: int,
    profile_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Ferramentas de investigação sem capacidade de mutar o escopo do usuário."""

    tools = build_tool_schemas(
        allow_shell=False,
        allow_write=False,
        allow_subagents=allow_subagents,
        subagent_max_steps=subagent_max_steps,
        profile_names=profile_names,
    )
    tools.extend(
        [
            {
                "type": "function",
                "function": {
                    "name": "ask_user_question",
                    "description": (
                        "Interrompe o planejamento para obter do operador uma decisão que não deve ser inferida. "
                        "A resposta volta como UserQuestion tipada."
                    ),
                    "parameters": AskUserQuestionArguments.model_json_schema(),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_plan",
                    "description": "Submete o plano final tipado para validação Pydantic e persistência local.",
                    "parameters": SubmitPlanArguments.model_json_schema(),
                },
            },
        ]
    )
    return tools


def build_plan_execution_tool_schemas(
    *,
    allow_shell: bool,
    allow_subagents: bool,
    subagent_max_steps: int,
    profile_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    tools = build_tool_schemas(
        allow_shell=allow_shell,
        allow_write=True,
        allow_subagents=allow_subagents,
        subagent_max_steps=subagent_max_steps,
        profile_names=profile_names,
    )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "request_scope_expansion",
                "description": (
                    "Solicita replanejamento ANTES de qualquer ação material fora do plano aprovado. "
                    "Após esta call, o runtime bloqueia as demais tools até nova aprovação humana."
                ),
                "parameters": RequestScopeExpansionArguments.model_json_schema(),
            },
        }
    )
    return tools


def create_system_prompt(config: AgentConfig) -> str:
    prompt = build_main_system_prompt(
        config,
        read_scope_description=READ_SCOPES[config.read_scope],
        write_scope_description=WRITE_SCOPES[config.write_scope],
        permission_mode_description=PERMISSION_MODES[config.permission_mode],
        verbosity_mode_description=VERBOSITY_MODES[config.verbosity_mode],
        verbosity_instruction=verbosity_style_instruction,
    )
    criteria = "\n".join(f"- {item}" for item in config.user_profile_criteria)
    skills = ", ".join(config.user_profile_skills) or "nenhuma preferência"
    return (
        f"{prompt}\n\n<user_profile id=\"{config.user_profile_id}\">\n"
        f"Foco (WHAT): {config.user_profile_what}\n"
        f"Critérios:\n{criteria or '- seguir os critérios gerais do runtime'}\n"
        f"Skills preferenciais: {skills}.\n"
        "Este overlay orienta análise e saída; nunca amplia permissões, tools, modelo ou escopo.\n"
        "</user_profile>"
    )

def append_profile_catalog(system_prompt: str, profiles: dict[str, AgentProfile]) -> str:
    return (
        f"{system_prompt}\n\nPersonalidades de subagentes disponíveis:\n"
        f"{format_agent_profile_catalog(profiles)}\n\n"
        "Os manifests refinam personalidade, capabilities, contexto, reasoning, limites e contrato de saída. "
        "Eles nunca alteram modelo/endpoint nem concedem mutação sem grant explícito do runtime. "
        "Descreva necessidades por capabilities, sem escolher agentes. Para implementação especializada, use a "
        "capability do especialista sem acrescentar code.implement de forma redundante; reserve code.implement "
        "para implementação generalista. A seleção final pertence ao runtime Python."
    )


def read_context_file(path: Path) -> str:
    return load_context_file(path, max_bytes=MAX_CONTEXT_FILE_BYTES)


def read_saved_history_context(config: AgentConfig) -> str:
    return load_saved_history_context(
        config,
        history_dir_name=HISTORY_DIR_NAME,
        max_history_file_bytes=MAX_HISTORY_FILE_BYTES,
    )


def read_project_context(
    config: AgentConfig,
    *,
    skill_catalog: str = "",
    spec_context: str = "",
) -> str:
    return assemble_project_context(
        config,
        skill_catalog=skill_catalog,
        spec_context=spec_context,
        max_context_file_bytes=MAX_CONTEXT_FILE_BYTES,
        max_context_total_bytes=MAX_CONTEXT_TOTAL_BYTES,
        history_dir_name=HISTORY_DIR_NAME,
        max_history_file_bytes=MAX_HISTORY_FILE_BYTES,
    )


def build_local_skill_registry(config: AgentConfig) -> SkillRegistry | None:
    if not config.load_project_context or not config.skills_dir.is_dir():
        return None
    return SkillRegistry(config.skills_dir, boundary=config.skills_dir.parent)


def detect_spec_kit_context(config: AgentConfig) -> SpecKitContext | None:
    if not config.load_project_context:
        return None
    scan = SpecKitAdapter(config.workspace).scan()
    return scan.to_context() if scan.recognized else None

def read_subagent_context(
    config: AgentConfig,
    profile: AgentProfile | None,
    skill_registry: SkillRegistry | None = None,
) -> str:
    return assemble_subagent_context(
        config,
        profile,
        skill_registry=skill_registry or build_local_skill_registry(config),
        max_context_file_bytes=MAX_CONTEXT_FILE_BYTES,
        max_context_total_bytes=MAX_CONTEXT_TOTAL_BYTES,
        history_dir_name=HISTORY_DIR_NAME,
        max_history_file_bytes=MAX_HISTORY_FILE_BYTES,
    )

def create_initial_messages(
    config: AgentConfig,
    agent_profiles: dict[str, AgentProfile] | None = None,
    *,
    skill_registry: SkillRegistry | None = None,
    spec_context: SpecKitContext | None = None,
) -> list[Message]:
    profiles = agent_profiles or {}
    system_prompt = append_profile_catalog(create_system_prompt(config), profiles)
    project_context = read_project_context(config)
    if project_context:
        system_prompt = (
            f"{system_prompt}\n\n"
            "Contexto local carregado a partir de AGENTS.md e históricos salvos. "
            "Use esse contexto como orientação operacional, respeitando as instruções do usuário e os limites de segurança.\n\n"
            f"{project_context}"
        )
    messages: list[Message] = [{"role": "system", "content": system_prompt}]
    if spec_context is not None:
        messages.append({"role": "system", "content": f"<spec_context>\n{spec_context.to_prompt()}\n</spec_context>"})
    if skill_registry is not None:
        active_skills = set(config.user_profile_skills)
        catalog = [
            item.to_dict()
            for item in skill_registry.list_metadata()
            if not active_skills or item.name in active_skills
        ]
        messages.append(
            {
                "role": "system",
                "content": (
                    "<skill_context level=\"0\">\n"
                    "Catálogo de metadata; use /skills ou seleção explícita para carregar L1/L2.\n"
                    f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n</skill_context>"
                ),
            }
        )
    return messages


def create_subagent_messages(
    config: AgentConfig,
    name: str,
    task: str,
    scope: str,
    allow_mutation: bool,
    profile: AgentProfile | None = None,
    task_spec: TaskSpec | None = None,
) -> list[Message]:
    return assemble_subagent_messages(
        config,
        name=name,
        task=task,
        scope=scope,
        allow_mutation=allow_mutation,
        profile=profile,
        task_spec=task_spec,
        project_context=read_subagent_context(config, profile),
        result_function_name=SUBMIT_AGENT_RESULT_FUNCTION,
        read_scope_description=READ_SCOPES[config.read_scope],
        write_scope_description=WRITE_SCOPES[config.write_scope],
        permission_mode_description=PERMISSION_MODES[config.permission_mode],
        verbosity_mode_description=VERBOSITY_MODES[config.verbosity_mode],
        verbosity_instruction=verbosity_style_instruction,
    )

def print_help(config: AgentConfig) -> None:
    console = get_console()
    if console is not None and Table is not None and Panel is not None and box is not None:
        command_table = Table(
            show_header=True,
            header_style=RICH_STYLE_BY_NAME["yellow"],
            box=box.SIMPLE_HEAVY,
            expand=False,
        )
        command_table.add_column("Comando", style=RICH_STYLE_BY_NAME["yellow"], no_wrap=True)
        command_table.add_column("Descrição", style=RICH_STYLE_BY_NAME["white"])
        for command, description in SLASH_COMMANDS.items():
            command_table.add_row(command, description)
        console.print(
            Panel(
                command_table,
                title="Comandos locais",
                subtitle=f"perfil {config.user_profile_id} • modo {config.permission_mode} • /status para detalhes",
                border_style=RICH_STYLE_BY_NAME["yellow"],
            )
        )
        return

    width = max(len(command) for command in SLASH_COMMANDS) + 2
    rows = [f"  {command:<{width}}{description}" for command, description in SLASH_COMMANDS.items()]
    inner_width = max(len(row) for row in rows)
    print_styled("+" + "-" * (inner_width + 2) + "+", style="yellow")
    print_styled("| " + "Comandos locais".ljust(inner_width) + " |", style="yellow")
    print_styled("+" + "-" * (inner_width + 2) + "+", style="yellow")
    for row in rows:
        print_styled("| " + row.ljust(inner_width) + " |", style="yellow")
    footer = f"perfil {config.user_profile_id} | modo {config.permission_mode} | /status para detalhes"
    print_styled("+" + "-" * (inner_width + 2) + "+", style="yellow")
    print_styled("  " + footer, style="gray")


def serialize_tool_call(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        },
    }


def parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    if not raw_arguments:
        return {}
    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValueError("Argumentos da ferramenta precisam ser um objeto JSON.")
    return parsed




def print_tool_activity(tool_name: str, arguments: dict[str, Any], step: int, max_steps: int) -> None:
    print_labeled("Atividade>", describe_tool_activity(tool_name, arguments, step, max_steps), style="cyan")


def print_tool_result(tool_name: str, result: str) -> None:
    style, summary = summarize_tool_result(tool_name, result)
    print_labeled("Resultado>", summary, style=style, content_style=style)




def print_assistant(content: str) -> None:
    rendered = plain_terminal_markdown(content)
    print_labeled("Assistente>", rendered, style="cyan", content_style=infer_assistant_content_style(rendered))


def match_slash_commands(prefix: str) -> list[str]:
    candidate = prefix.strip()
    if not candidate.startswith("/"):
        return []
    commands = [*SLASH_COMMANDS.keys(), *SLASH_ALIASES.keys()]
    return sorted(command for command in commands if command.startswith(candidate))


def print_slash_suggestions(prefix: str) -> None:
    matches = match_slash_commands(prefix)
    console = get_console()
    if not matches and console is not None:
        print_styled(f"Nenhum comando local começa com {prefix!r}.", style="yellow")
        return
    if matches and console is not None and Table is not None and box is not None:
        table = Table(show_header=True, header_style=RICH_STYLE_BY_NAME["yellow"], box=box.SIMPLE, expand=False)
        table.add_column("Comando", style=RICH_STYLE_BY_NAME["yellow"], no_wrap=True)
        table.add_column("Descrição", style=RICH_STYLE_BY_NAME["yellow"])
        for command in matches:
            canonical = SLASH_ALIASES.get(command, command)
            description = SLASH_COMMANDS.get(canonical, f"Alias de {canonical}")
            alias_suffix = f" -> {canonical}" if canonical != command else ""
            table.add_row(command, f"{description}{alias_suffix}")
        console.print(table)
        return
    if not matches:
        print_styled(f"Nenhum comando local começa com {prefix!r}.", style="yellow")
        return
    print_styled("Comandos correspondentes:", style="yellow")
    for command in matches:
        canonical = SLASH_ALIASES.get(command, command)
        description = SLASH_COMMANDS.get(canonical, f"Alias de {canonical}")
        alias_suffix = f" -> {canonical}" if canonical != command else ""
        print(f"  {command:<13} {description}{alias_suffix}")


def is_incomplete_slash_command(user_input: str) -> bool:
    value = user_input.strip()
    if not value.startswith("/"):
        return False
    if value in SLASH_COMMANDS or value in SLASH_ALIASES:
        return False
    return bool(match_slash_commands(value))


def common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while not value.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def format_inline_suggestions(buffer: str) -> str:
    matches = match_slash_commands(buffer)
    if not matches:
        return ""
    visible = ", ".join(matches[:8])
    suffix = " ..." if len(matches) > 8 else ""
    return f" {YELLOW}[{visible}{suffix}]{RESET}"


class SlashCommandCompleter(Completer if Completer is not None else object):
    def get_completions(self, document: Any, complete_event: Any) -> Any:
        if Completion is None:
            return

        candidate = document.text_before_cursor.strip()
        if not candidate.startswith("/"):
            return

        for command in match_slash_commands(candidate):
            canonical = SLASH_ALIASES.get(command, command)
            description = SLASH_COMMANDS.get(canonical, f"Alias de {canonical}")
            yield Completion(
                command,
                start_position=-len(candidate),
                display=command,
                display_meta=description,
            )

    async def get_completions_async(self, document: Any, complete_event: Any) -> Any:
        for completion in self.get_completions(document, complete_event):
            yield completion


def build_prompt_session() -> Any | None:
    if PromptSession is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        return PromptSession(
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
            reserve_space_for_menu=8,
        )
    except Exception:
        # Some Windows-hosted runners expose a TTY-like stream without a real console buffer.
        return None


def read_user_input(prompt: str, prompt_session: Any | None = None) -> str:
    if prompt_session is not None:
        prompt_text = ANSI(prompt) if ANSI is not None else prompt
        return prompt_session.prompt(prompt_text)
    # The former per-character Windows fallback cleared only the current
    # physical line, so wrapped prompts were reprinted on every keypress.
    # prompt-toolkit is now an installed runtime dependency; plain input is a
    # safe degradation path when no real console is available.
    return input(prompt)


async def read_user_input_async(prompt: str, prompt_session: Any | None = None) -> str:
    """Read interactive input without nesting an event loop.

    ``PromptSession.prompt()`` owns its own ``asyncio.run()`` call.  The main
    runtime already executes inside an asyncio loop, so interactive sessions
    must use prompt-toolkit's native async API instead.
    """

    if prompt_session is not None:
        prompt_text = ANSI(prompt) if ANSI is not None else prompt
        return await prompt_session.prompt_async(prompt_text)
    return await asyncio.to_thread(input, prompt)


def normalize_permission_mode(value: str) -> str:
    aliases = {
        "restrito": "strict",
        "sempre": "strict",
        "supervised": "strict",
        "equilibrado": "balanced",
        "seguro": "balanced",
        "risky": "balanced",
        "livre": "auto",
        "automatico": "auto",
        "autônomo": "auto",
        "autonomo": "auto",
        "autopilot": "auto",
    }
    normalized = value.strip().lower()
    normalized = aliases.get(normalized, normalized)
    if normalized not in PERMISSION_MODES:
        raise ValueError(f"Modo inválido: {value}. Use strict, balanced ou auto.")
    return normalized


def print_permission_mode(config: AgentConfig) -> None:
    console = get_console()
    if console is not None and Table is not None and box is not None:
        print_labeled("Modo atual:", f"{config.permission_mode} - {PERMISSION_MODES[config.permission_mode]}", style="cyan")
        table = Table(show_header=True, header_style=RICH_STYLE_BY_NAME["yellow"], box=box.SIMPLE, expand=False)
        table.add_column("Modo", style=RICH_STYLE_BY_NAME["yellow"], no_wrap=True)
        table.add_column("Descrição", style=RICH_STYLE_BY_NAME["yellow"])
        for mode_name in ("strict", "balanced", "auto"):
            table.add_row(mode_name, PERMISSION_MODES[mode_name])
        console.print(table)
        return

    print_labeled("Modo atual:", f"{config.permission_mode} - {PERMISSION_MODES[config.permission_mode]}", style="cyan")
    print(
        to_json(
            {
                "strict": PERMISSION_MODES["strict"],
                "balanced": PERMISSION_MODES["balanced"],
                "auto": PERMISSION_MODES["auto"],
            }
        )
    )


def normalize_deep_mode(value: str) -> ReasoningMode:
    normalized = value.strip().lower()
    if normalized in {"on", "ligado", "ativado", "1", "true"}:
        return ReasoningMode.DEEP
    if normalized in {"off", "desligado", "desativado", "0", "false"}:
        return ReasoningMode.NORMAL
    raise ValueError("Use /deep on, /deep off ou /deep status.")


def resolve_deep_command(value: str, current: ReasoningMode) -> tuple[ReasoningMode, bool]:
    """Resolve on/off/status; the boolean reports whether session state changed."""

    normalized = value.strip().lower()
    if not normalized or normalized == "status":
        return current, False
    return normalize_deep_mode(normalized), True


def print_deep_mode(reasoning_mode: ReasoningMode) -> None:
    enabled = reasoning_mode is ReasoningMode.DEEP
    effort = "max" if enabled else "none"
    print_labeled(
        "Deep Thinking:",
        f"{'on' if enabled else 'off'} (reasoning_effort={effort})",
        style="cyan",
    )


def normalize_verbosity_mode(value: str) -> str:
    aliases = {
        "curto": "direto",
        "conciso": "direto",
        "resumido": "direto",
        "short": "direto",
        "brief": "direto",
        "padrao": "normal",
        "padrão": "normal",
        "default": "normal",
        "medio": "normal",
        "médio": "normal",
        "completo": "detalhado",
        "detalhada": "detalhado",
        "verboso": "detalhado",
        "verbose": "detalhado",
        "long": "detalhado",
    }
    normalized = value.strip().lower()
    normalized = aliases.get(normalized, normalized)
    if normalized not in VERBOSITY_MODES:
        raise ValueError(f"Verbosidade inválida: {value}. Use direto, normal ou detalhado.")
    return normalized


def verbosity_style_instruction(mode: str) -> str:
    if mode == "direto":
        return (
            "Responda no menor tamanho útil. Priorize conclusão, ação executada, validação e bloqueios. "
            "Evite explicações de fundo, listas longas e detalhes que o usuário não pediu."
        )
    if mode == "detalhado":
        return (
            "Inclua contexto, critérios, premissas, riscos, validação e próximos passos quando forem relevantes. "
            "Não repita informação, não alongue por formalidade e mantenha a resposta acionável."
        )
    return (
        "Equilibre objetividade e contexto. Explique o suficiente para o usuário decidir ou continuar, "
        "sem transformar respostas simples em relatórios."
    )


def print_verbosity_mode(config: AgentConfig) -> None:
    console = get_console()
    if console is not None and Table is not None and box is not None:
        print_labeled("Verbosidade atual:", f"{config.verbosity_mode} - {VERBOSITY_MODES[config.verbosity_mode]}", style="cyan")
        table = Table(show_header=True, header_style=RICH_STYLE_BY_NAME["yellow"], box=box.SIMPLE, expand=False)
        table.add_column("Modo", style=RICH_STYLE_BY_NAME["yellow"], no_wrap=True)
        table.add_column("Descrição", style=RICH_STYLE_BY_NAME["yellow"])
        for mode_name in ("direto", "normal", "detalhado"):
            table.add_row(mode_name, VERBOSITY_MODES[mode_name])
        console.print(table)
        return

    print_labeled("Verbosidade atual:", f"{config.verbosity_mode} - {VERBOSITY_MODES[config.verbosity_mode]}", style="cyan")
    print(
        to_json(
            {
                "direto": VERBOSITY_MODES["direto"],
                "normal": VERBOSITY_MODES["normal"],
                "detalhado": VERBOSITY_MODES["detalhado"],
            }
        )
    )


async def emit_workflow_state(
    event_bus: EventBus | None,
    *,
    workflow: str,
    status: str,
    plan: Plan | None = None,
    goal: Goal | None = None,
) -> None:
    if event_bus is None:
        return
    payload: dict[str, Any] = {"workflow": workflow, "status": status}
    if plan is not None:
        payload.update(plan_id=plan.reference, plan_revision=plan.revision)
    if goal is not None:
        payload["goal_id"] = goal.goal_id
    await event_bus.emit("workflow.state_changed", source=AGENT_NAME, payload=payload)


async def _resolve_callback(callback: Any, *args: Any) -> Any:
    result = callback(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result


def _question_prompt(question: UserQuestion) -> str:
    options = ""
    if question.options:
        options = "\n" + "\n".join(
            f"  {index}. {option}" for index, option in enumerate(question.options, start=1)
        )
    return f"{question.prompt}{options}\nResposta> "


class PlanningWorkspaceTools:
    """Capability boundary for `/plan`: investigation plus typed planning only."""

    _MUTATING_TOOLS = frozenset({"write_file", "run_cli", "run_powershell"})

    def __init__(
        self,
        base: WorkspaceTools,
        store: PlanStore,
        plan_id: str,
        *,
        question_provider: Any = None,
        base_plan: Plan | None = None,
        revision_reason: str | None = None,
        pending_scope_expansion: ScopeExpansion | None = None,
        exploration: ExplorationPreparation | None = None,
    ) -> None:
        self.base = base
        self.config = base.config
        self.store = store
        self.plan_id = plan_id
        self.question_provider = question_provider
        self.base_plan = base_plan
        self.revision_reason = revision_reason
        self.pending_scope_expansion = pending_scope_expansion
        self.exploration = exploration
        self.questions: list[UserQuestion] = []
        self.submitted_plan: Plan | None = None

    async def _ask_user(self, arguments: dict[str, Any]) -> str:
        question = AskUserQuestionArguments.model_validate_json(to_json(arguments)).question
        if question.status is not UserQuestionStatus.PENDING:
            raise ValueError("ask_user_question exige UserQuestion com status=pending.")
        await emit_workflow_state(
            self.base.event_bus,
            workflow="plan",
            status=PlanLifecycleStatus.WAITING_USER.value,
            plan=self.base_plan,
        )
        if self.question_provider is None:
            try:
                answer = read_user_input(_question_prompt(question))
            except EOFError as exc:
                raise ApprovalUnavailableError("Pergunta do planner sem entrada disponível.") from exc
        else:
            answer = await _resolve_callback(self.question_provider, question)
        normalized_answer = str(answer or "").strip()
        if question.required and not normalized_answer:
            raise ValueError("A pergunta obrigatória do planner precisa de resposta.")
        answered = UserQuestion.model_validate(
            {
                **question.model_dump(mode="python"),
                "status": (
                    UserQuestionStatus.ANSWERED if normalized_answer else UserQuestionStatus.SKIPPED
                ),
                "answer": normalized_answer or None,
            }
        )
        self.questions.append(answered)
        await emit_workflow_state(
            self.base.event_bus,
            workflow="plan",
            status=PlanLifecycleStatus.PLANNING.value,
            plan=self.base_plan,
        )
        return answered.model_dump_json()

    async def _submit_plan(self, arguments: dict[str, Any]) -> str:
        proposed = SubmitPlanArguments.model_validate_json(to_json(arguments)).plan
        if not proposed.tasks:
            raise ValueError("O plano precisa conter pelo menos uma TaskSpec.")
        pending_required = [
            question.question_id
            for question in self.questions
            if question.required and question.status is not UserQuestionStatus.ANSWERED
        ]
        if pending_required:
            raise ValueError(f"Perguntas obrigatórias ainda pendentes: {pending_required}")

        now = datetime.now(timezone.utc)
        payload = proposed.model_dump(mode="python")
        prior_expansions = list(self.base_plan.scope_expansions if self.base_plan is not None else [])
        if self.pending_scope_expansion is not None:
            missing_scope = [
                item
                for item in self.pending_scope_expansion.added_scope
                if item not in {*proposed.scope, *proposed.planned_areas, *proposed.planned_files}
            ]
            if missing_scope:
                raise ValueError(
                    "O plano revisado precisa incorporar toda a expansão de escopo proposta: "
                    f"{missing_scope}"
                )
            prior_expansions.append(self.pending_scope_expansion)
        payload.update(
            plan_id=self.plan_id,
            questions=[
                *(self.base_plan.questions if self.base_plan is not None else []),
                *self.questions,
            ],
            approvals=[],
            scope_expansions=prior_expansions,
            status=PlanLifecycleStatus.WAITING_APPROVAL,
            created_at=(self.base_plan.created_at if self.base_plan is not None else now),
            updated_at=now,
            revision=(self.base_plan.revision if self.base_plan is not None else 1),
            revision_history=(self.base_plan.revision_history if self.base_plan is not None else []),
            metadata={
                **(self.base_plan.metadata if self.base_plan is not None else {}),
                **proposed.metadata,
                **(
                    {
                        "code_intelligence": {
                            "admission": self.exploration.decision.level.value,
                            "reasons": list(self.exploration.decision.reasons),
                            "report_id": self.exploration.run.report.report_id,
                            "exploration_artifact": self.exploration.run.artifacts.exploration_report,
                            "architecture_graph_artifact": self.exploration.run.artifacts.architecture_graph,
                            "execution_flow_artifact": self.exploration.run.artifacts.execution_flow,
                            "symbol_snapshot_artifact": self.exploration.run.artifacts.symbol_snapshot,
                            "index_updated_at": self.exploration.run.report.index_updated_at.isoformat(),
                        }
                    }
                    if self.exploration is not None and self.exploration.run is not None
                    else {}
                ),
            },
        )
        candidate = Plan.model_validate(payload)
        if self.base_plan is None:
            saved = self.store.save(candidate)
        else:
            saved = self.store.revise(
                candidate,
                reason=self.revision_reason or "Revisão solicitada pelo operador.",
                expected_revision=self.base_plan.revision,
            )
        self.submitted_plan = saved
        if self.base.event_bus is not None:
            await self.base.event_bus.emit(
                "plan.persisted",
                source=AGENT_NAME,
                payload={
                    "plan_id": saved.reference,
                    "plan_revision": saved.revision,
                    "status": saved.status.value,
                },
            )
        return saved.model_dump_json()

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        execution_context: str = "",
    ) -> str:
        if name in self._MUTATING_TOOLS:
            return to_json(
                {
                    "error": "PlanReadOnlyViolation",
                    "message": f"{name} não é permitido durante /plan.",
                }
            )
        if name == "ask_user_question":
            return await self._ask_user(arguments)
        if name == "submit_plan":
            return await self._submit_plan(arguments)
        if name == "spawn_subagent" and bool(arguments.get("allow_mutation")):
            return to_json(
                {
                    "error": "PlanReadOnlyViolation",
                    "message": "Subagente do planner precisa ser read-only.",
                }
            )
        if name == "delegate_task":
            try:
                request = DelegateTaskArguments.model_validate(arguments)
            except ValidationError as exc:
                return to_json({"error": "ValueError", "message": str(exc)})
            if not request.task_spec.read_only or request.allow_mutation:
                return to_json(
                    {
                        "error": "PlanReadOnlyViolation",
                        "message": "TaskSpec do planner precisa ter read_only=true.",
                    }
                )
        return await self.base.execute_async(name, arguments, execution_context=execution_context)


class PlanExecutionWorkspaceTools:
    """Binds execution tools to one approved revision and halts on expansion."""

    _SCOPED_MUTATION_TOOLS = frozenset({"write_file", "run_cli", "run_powershell", "spawn_subagent"})

    def __init__(
        self,
        base: WorkspaceTools,
        store: PlanStore,
        plan: Plan,
        *,
        scope_state: dict[str, ScopeExpansion | None] | None = None,
    ) -> None:
        self.base = base
        self.config = base.config
        self.store = store
        self.plan = plan
        self._scope_state = scope_state if scope_state is not None else {"pending": None}

    @property
    def pending_scope_expansion(self) -> ScopeExpansion | None:
        return self._scope_state["pending"]

    @pending_scope_expansion.setter
    def pending_scope_expansion(self, value: ScopeExpansion | None) -> None:
        self._scope_state["pending"] = value

    def child(self, base: WorkspaceTools) -> "PlanExecutionWorkspaceTools":
        """Create a subagent boundary sharing the same approved scope-expansion state."""

        return PlanExecutionWorkspaceTools(
            base,
            self.store,
            self.plan,
            scope_state=self._scope_state,
        )

    def _approved_task(self, requested: TaskSpec) -> bool:
        return any(task == requested for task in self.plan.tasks)

    @staticmethod
    def _scope_expansion_error(message: str) -> str:
        return to_json(
            {
                "error": "ScopeExpansionApprovalRequired",
                "message": f"{message} Use request_scope_expansion antes da ação.",
            }
        )

    def _planned_tool(self, name: str) -> bool:
        normalized = {item.strip().lower() for item in self.plan.planned_tools}
        return name.lower() in normalized

    def _planned_write_path(self, raw_path: str, path_reference: str = "") -> bool:
        try:
            requested = self.base.resolve_user_path(raw_path, path_reference)
        except (OSError, PermissionError, ValueError):
            return False
        candidates = [*self.plan.planned_files, *self.plan.planned_areas, *self.plan.scope]
        for candidate in candidates:
            value = candidate.strip()
            if not value:
                continue
            try:
                boundary = self.base.resolve_user_path(value)
            except (OSError, PermissionError, ValueError):
                continue
            if requested == boundary or is_path_inside_workspace(boundary, requested):
                return True
        return False

    def _planned_command(self, command: str) -> bool:
        normalized = " ".join(command.lower().split())
        for planned in self.plan.planned_commands:
            allowed = " ".join(planned.lower().split())
            if allowed and normalized == allowed:
                return True
        return False

    async def _request_scope_expansion(self, arguments: dict[str, Any]) -> str:
        expansion = RequestScopeExpansionArguments.model_validate_json(to_json(arguments)).expansion
        if expansion.status is not ScopeExpansionStatus.PROPOSED or expansion.approval is not None:
            raise ValueError("request_scope_expansion exige ScopeExpansion proposta e ainda não aprovada.")
        self.pending_scope_expansion = expansion
        if self.base.event_bus is not None:
            await self.base.event_bus.emit(
                "plan.scope_expansion_requested",
                source=AGENT_NAME,
                payload={
                    "plan_id": self.plan.reference,
                    "plan_revision": self.plan.revision,
                    "status": PlanLifecycleStatus.REQUIRES_REPLANNING.value,
                },
            )
        await emit_workflow_state(
            self.base.event_bus,
            workflow="plan",
            status=PlanLifecycleStatus.REQUIRES_REPLANNING.value,
            plan=self.plan,
        )
        return to_json(
            {
                "status": "requires_replanning",
                "plan_id": self.plan.reference,
                "scope_expansion": expansion.model_dump(mode="json"),
                "message": "Execução pausada; a expansão material exige replanejamento e nova aprovação humana.",
            }
        )

    async def delegate_task(
        self,
        task_spec: TaskSpec,
        *,
        dependencies_resolved: bool,
        execution_context: str,
    ) -> str:
        """Delegate one exact approved TaskSpec through the scoped subagent boundary."""

        if self.pending_scope_expansion is not None:
            return self._scope_expansion_error(
                "Nenhuma delegação pode iniciar enquanto há expansão de escopo pendente."
            )
        if not self._approved_task(task_spec):
            return self._scope_expansion_error(
                "TaskSpec delegada não corresponde ao plano aprovado."
            )
        return await self.base.delegate_task(
            task_spec,
            allow_mutation=not task_spec.read_only,
            runtime_mutation_grant=not task_spec.read_only,
            dependencies_resolved=dependencies_resolved,
            execution_context=execution_context,
            execution_scope=self,
        )

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        execution_context: str = "",
    ) -> str:
        if self.pending_scope_expansion is not None:
            return to_json(
                {
                    "error": "ScopeExpansionApprovalRequired",
                    "plan_id": self.plan.reference,
                    "message": "Nenhuma outra tool pode executar antes do replanejamento e da nova aprovação.",
                }
            )
        if name == "request_scope_expansion":
            return await self._request_scope_expansion(arguments)
        if name in self._SCOPED_MUTATION_TOOLS and not self._planned_tool(name):
            return self._scope_expansion_error(f"A tool {name} não consta no plano aprovado.")
        if name == "write_file" and not self._planned_write_path(
            str(arguments.get("path") or ""),
            str(arguments.get("path_reference") or ""),
        ):
            return self._scope_expansion_error("O caminho de escrita não consta no escopo aprovado.")
        if name == "run_cli":
            cli = str(arguments.get("cli") or "")
            raw_args = arguments.get("args") or []
            command = subprocess.list2cmdline([cli, *raw_args]) if isinstance(raw_args, list) else cli
            if not self._planned_command(command):
                return self._scope_expansion_error("A CLI solicitada não consta nos comandos aprovados.")
        if name == "run_powershell" and not self._planned_command(str(arguments.get("command") or "")):
            return self._scope_expansion_error("O comando PowerShell não consta nos comandos aprovados.")
        if name == "spawn_subagent" and bool(arguments.get("allow_mutation")):
            return self._scope_expansion_error(
                "Delegação mutável precisa usar delegate_task com a TaskSpec aprovada."
            )
        if name == "delegate_task":
            try:
                request = DelegateTaskArguments.model_validate(arguments)
            except ValidationError as exc:
                return to_json({"error": "ValueError", "message": str(exc)})
            if not self._approved_task(request.task_spec):
                return self._scope_expansion_error(
                    "TaskSpec delegada não corresponde ao plano aprovado."
                )
            authorized_arguments = dict(arguments)
            authorized_arguments["allow_mutation"] = not request.task_spec.read_only
            return await self.base.execute_async(
                name,
                authorized_arguments,
                runtime_mutation_grant=not request.task_spec.read_only,
                execution_scope=self,
                execution_context=execution_context,
            )
        return await self.base.execute_async(name, arguments, execution_context=execution_context)


async def run_plan_workflow(
    *,
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    tools_runner: WorkspaceTools,
    store: PlanStore,
    temperature: float,
    config: AgentConfig,
    objective: str,
    event_bus: EventBus | None = None,
    question_provider: Any = None,
    base_plan: Plan | None = None,
    revision_reason: str | None = None,
    pending_scope_expansion: ScopeExpansion | None = None,
    spec_workflow: SpecWorkflowContext | None = None,
    exploration: ExplorationPreparation | None = None,
) -> Plan:
    plan_id = base_plan.plan_id if base_plan is not None else store.next_plan_id()
    reference = plan_id if base_plan is None else f"{plan_id}-r{base_plan.revision + 1}"
    await emit_workflow_state(
        event_bus,
        workflow="plan",
        status=PlanLifecycleStatus.PLANNING.value,
        plan=base_plan,
    )
    planning_tools = PlanningWorkspaceTools(
        tools_runner,
        store,
        plan_id,
        question_provider=question_provider,
        base_plan=base_plan,
        revision_reason=revision_reason,
        pending_scope_expansion=pending_scope_expansion,
        exploration=exploration,
    )
    planning_messages = [
        *messages,
        {
            "role": "user",
            "content": build_plan_prompt(
                objective,
                reference,
                base_plan,
                spec_directive=spec_workflow.directive if spec_workflow is not None else "",
                exploration_directive=exploration.directive if exploration is not None else "",
            ),
        },
    ]
    schemas = build_planning_tool_schemas(
        allow_subagents=config.max_subagents > 0,
        subagent_max_steps=config.subagent_max_steps,
        profile_names=sorted(tools_runner.agent_profiles),
    )
    await run_agent_until_final(
        client=client,
        model=model,
        messages=planning_messages,
        tools_runner=planning_tools,
        tool_schemas=schemas,
        temperature=temperature,
        max_steps=config.max_steps,
        api_retries=config.api_retries,
        emit_tools=event_bus is not None,
        event_bus=event_bus,
        stream_output=event_bus is not None,
        reasoning_mode=ReasoningMode.DEEP,
    )
    if planning_tools.submitted_plan is None:
        raise ValueError("O planner encerrou sem chamar submit_plan com um Plan válido.")
    await emit_workflow_state(
        event_bus,
        workflow="plan",
        status=PlanLifecycleStatus.WAITING_APPROVAL.value,
        plan=planning_tools.submitted_plan,
    )
    return planning_tools.submitted_plan


async def request_plan_approval(plan: Plan, approval_provider: Any = None) -> tuple[str, Approval]:
    if approval_provider is None:
        print_assistant(format_plan_summary(plan))
        try:
            raw = read_user_input("[A] Aprovar  [R] Revisar  [C] Cancelar\nAprovação> ")
        except EOFError as exc:
            raise ApprovalUnavailableError("Aprovação obrigatória do plano sem entrada disponível.") from exc
    else:
        raw = await _resolve_callback(approval_provider, plan)
    return resolve_plan_approval(plan, raw)


async def run_independent_review(
    *,
    client: ModelAdapter,
    model: str,
    plan: Plan,
    scheduler_result: SchedulerResult,
    temperature: float,
    api_retries: int,
    event_bus: EventBus | None = None,
    reviewer: Any = None,
    repair_attempt: int = 0,
    exploration: ExplorationPreparation | None = None,
) -> ReviewResult:
    """Review a scheduler pass in isolated context with Deep Thinking forced."""

    if event_bus is not None:
        await event_bus.emit(
            "review.started",
            source="reviewer",
            payload={
                "plan_id": plan.reference,
                "plan_revision": plan.revision,
                "status": "reviewing",
                "repair_attempt": repair_attempt,
            },
        )
    known_task_ids = frozenset(task.task_id for task in plan.tasks)
    protocol = ReviewResultFunctionProtocol()

    async def emit_review_failure(status: str, exc: BaseException) -> None:
        if event_bus is None:
            return
        await event_bus.emit(
            "review.failed",
            source="reviewer",
            payload={
                "plan_id": plan.reference,
                "plan_revision": plan.revision,
                "status": status,
                "repair_attempt": repair_attempt,
                "error_type": type(exc).__name__,
            },
        )

    if reviewer is not None:
        try:
            supplied = await _resolve_callback(reviewer, plan, scheduler_result.states, repair_attempt)
            candidate = supplied if isinstance(supplied, ReviewResult) else ReviewResult.model_validate(supplied)
            return protocol.validate(
                SUBMIT_REVIEW_RESULT_FUNCTION,
                candidate.model_dump_json(),
                plan_id=plan.plan_id,
                plan_revision=plan.revision,
                known_task_ids=known_task_ids,
            )
        except Exception as exc:
            await emit_review_failure("reviewer_error", exc)
            raise

    review_messages: list[Message] = [
        {
            "role": "system",
            "content": (
                "Você é o Reviewer independente. Avalie evidências, estados das tasks e critérios de aceite. "
                "Use Deep Thinking. Não execute tools nem proponha expansão de escopo. Aprove somente se "
                "todas as tasks e critérios estiverem satisfatoriamente concluídos. Em rejeição, selecione em "
                "repair_task_ids apenas IDs existentes que precisam ser refeitos; dependentes serão incluídos "
                "deterministicamente pelo runtime. Finalize somente com submit_review_result."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Plano aprovado:\n{plan.model_dump_json(indent=2)}\n\n"
                + (
                    f"ExplorationReport válido reutilizado:\n{exploration.directive}\n\n"
                    if exploration is not None and exploration.run is not None
                    else ""
                )
                +
                "Estados e resultados do scheduler:\n"
                + to_json([state.model_dump(mode="json") for state in scheduler_result.states])
            ),
        },
    ]
    failure: ReviewProtocolError | None = None
    for protocol_attempt in range(REVIEW_PROTOCOL_REPAIRS + 1):
        try:
            response = await create_chat_completion_with_retry(
                client,
                operation="independent_review",
                api_retries=api_retries,
                emit_status=False,
                loading_enabled=False,
                request=ModelRequest(
                    messages=review_messages,
                    tools=[protocol.function_schema()],
                    tool_choice={
                        "type": "function",
                        "function": {"name": SUBMIT_REVIEW_RESULT_FUNCTION},
                    },
                    temperature=temperature,
                    reasoning_mode=ReasoningMode.DEEP,
                ),
                event_bus=event_bus,
                agent_name="reviewer",
                visible=False,
                use_stream=False,
            )
        except Exception as exc:
            await emit_review_failure("reviewer_error", exc)
            raise
        try:
            if len(response.tool_calls) != 1:
                raise ReviewProtocolError("Reviewer must return exactly one submit_review_result call.")
            tool_call = response.tool_calls[0]
            return protocol.validate(
                tool_call.name,
                tool_call.arguments,
                plan_id=plan.plan_id,
                plan_revision=plan.revision,
                known_task_ids=known_task_ids,
            )
        except ReviewProtocolError as exc:
            failure = exc
            if protocol_attempt >= REVIEW_PROTOCOL_REPAIRS:
                break
            review_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"ReviewResult inválido: {exc}. Faça somente o repair do protocolo e chame "
                        f"{SUBMIT_REVIEW_RESULT_FUNCTION}."
                    ),
                }
            )

    final_failure = failure or ReviewProtocolError("Reviewer did not produce a valid ReviewResult.")
    await emit_review_failure("protocol_failed", final_failure)
    raise final_failure


async def run_plan_execution(
    *,
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    tools_runner: WorkspaceTools,
    store: PlanStore,
    temperature: float,
    config: AgentConfig,
    plan: Plan,
    approval: Approval,
    event_bus: EventBus | None = None,
    reasoning_mode: ReasoningMode = ReasoningMode.NORMAL,
    task_executor: TaskExecutor | None = None,
    reviewer: Any = None,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
    goal: Goal | None = None,
    convergence_engine: ConvergenceEngine | None = None,
    analyze_result: AnalyzeResult | None = None,
    spec_workflow: SpecWorkflowContext | None = None,
    exploration: ExplorationPreparation | None = None,
    run_id: str | None = None,
    run_journal: RunJournal | None = None,
) -> tuple[str, ScopeExpansion | None]:
    current = store.load(plan.plan_id)
    if current.revision != plan.revision:
        raise PermissionError(
            f"Aprovação obsoleta: {plan.reference} não é mais a revisão atual ({current.reference})."
        )
    if (
        approval.decision is not ApprovalDecision.APPROVED
        or approval.plan_id != plan.plan_id
        or approval.plan_revision != plan.revision
    ):
        raise PermissionError("A execução exige Approval aprovado para a revisão exata do plano.")
    tools_runner.subagents_started = 0

    journal = run_journal or RunJournal(config.workspace)
    if run_id is None:
        durable_run = journal.create_run(plan, goal_id=goal.goal_id if goal is not None else None)
        resume_results: dict[str, AgentResult] = {}
    else:
        durable_run, recovery = journal.prepare_resume(run_id, plan=plan)
        if recovery.disposition is RecoveryDisposition.RECOVERY_REVIEW_REQUIRED:
            raise PermissionError(
                "Resume exige revalidação das tasks com efeito externo ambíguo: "
                + ", ".join(recovery.recovery_review_task_ids)
            )
        resume_results = journal.seed_completed_results(run_id, plan=plan)
    durable_run_id = durable_run.run_id

    if max_repairs < 0 or max_repairs > MAX_REPAIR_ATTEMPTS:
        raise ValueError(f"max_repairs precisa estar entre 0 e {MAX_REPAIR_ATTEMPTS}.")

    await emit_workflow_state(
        event_bus,
        workflow="plan",
        status=PlanLifecycleStatus.RUNNING.value,
        plan=plan,
    )
    if spec_workflow is not None and spec_workflow.decision.use_full_workflow:
        await emit_spec_workflow_stage(
            event_bus,
            SpecWorkflowStage.IMPLEMENT,
            status="running",
            official_artifacts=spec_workflow.decision.official_artifacts,
        )
    scoped_tools = PlanExecutionWorkspaceTools(tools_runner, store, plan)
    schemas = build_plan_execution_tool_schemas(
        allow_shell=config.allow_shell,
        allow_subagents=config.max_subagents > 0,
        subagent_max_steps=config.subagent_max_steps,
        profile_names=sorted(tools_runner.agent_profiles),
    )
    result_protocol = AgentResultFunctionProtocol(max_repairs=1)
    schemas.append(result_protocol.function_schema())
    dependency_results: dict[str, AgentResult] = dict(resume_results)
    repair_feedback = ""

    async def _execute_task_body(spec: TaskSpec, attempt: int) -> AgentResult:
        if task_executor is not None:
            supplied = await _resolve_callback(task_executor, spec, attempt)
            result = supplied if isinstance(supplied, AgentResult) else AgentResult.model_validate(supplied)
            if result.status is AgentResultStatus.COMPLETED:
                dependency_results[result.task_id] = result
            return result

        dependency_context = [
            dependency_results[dependency].model_dump(mode="json")
            for dependency in spec.dependencies
            if dependency in dependency_results
        ]
        scheduler_context = (
            f"TaskSpec aprovada: {spec.model_dump_json(indent=2)}\n"
            f"Resultados das dependências: {to_json(dependency_context)}\n"
            f"Feedback do Reviewer para repair: {repair_feedback or 'nenhum'}"
        )
        if config.max_subagents > 0:
            delegated_payload = await scoped_tools.delegate_task(
                spec,
                dependencies_resolved=True,
                execution_context=scheduler_context,
            )
            delegated_result = aggregate_delegated_task_result(spec, delegated_payload)
            if delegated_result is not None:
                if delegated_result.status is AgentResultStatus.COMPLETED:
                    dependency_results[delegated_result.task_id] = delegated_result
                return delegated_result

        execution_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    f"Execute somente a TaskSpec aprovada {spec.task_id}, tentativa {attempt}.\n"
                    f"TaskSpec:\n{spec.model_dump_json(indent=2)}\n"
                    f"Resultados das dependências:\n{to_json(dependency_context)}\n"
                    f"Feedback do Reviewer para repair, se houver:\n{repair_feedback or 'nenhum'}\n\n"
                    "O DAG Scheduler Python já resolveu dependências e locks; não altere estados nem execute outra task. "
                    "Use delegate_task quando a admissão tipada indicar um subagente adequado, ou execute localmente. "
                    "Respeite o /mode e o plano aprovado. Se surgir expansão material, chame "
                    "request_scope_expansion ANTES da ação e pare. Finalize exclusivamente com "
                    "submit_agent_result conforme AgentResult."
                ),
            },
        ]
        try:
            async with asyncio.timeout(spec.limits.timeout_seconds):
                raw_result = await run_agent_until_final(
                    client=client,
                    model=model,
                    messages=execution_messages,
                    tools_runner=scoped_tools,
                    tool_schemas=schemas,
                    temperature=temperature,
                    max_steps=min(config.max_steps, spec.limits.max_steps),
                    api_retries=config.api_retries,
                    emit_tools=event_bus is not None,
                    event_bus=event_bus,
                    agent_name=f"worker:{spec.task_id}",
                    emit_run=False,
                    stream_output=False,
                    reasoning_mode=reasoning_mode,
                    result_protocol=result_protocol,
                    result_task_id=spec.task_id,
                )
            result = AgentResult.model_validate_json(raw_result)
            if result.status is AgentResultStatus.COMPLETED:
                dependency_results[result.task_id] = result
            return result
        except TimeoutError:
            return AgentResult(
                task_id=spec.task_id,
                status=AgentResultStatus.FAILED,
                summary="A task excedeu o timeout controlado pelo scheduler.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.TIMEOUT,
                        message=f"Execução excedeu {spec.limits.timeout_seconds} segundos.",
                        retryable=True,
                    )
                ],
            )

    async def execute_task(spec: TaskSpec, attempt: int) -> AgentResult:
        """Persist the task boundary around the scheduler-owned executor.

        RUNNING is published before any callback/model work.  A cancellation or
        exception therefore leaves that task recoverable in the journal; a
        returned result is persisted immediately and reconciled again from the
        scheduler result below.
        """

        journal.mark_task_started(
            durable_run_id,
            spec,
            attempt=attempt,
            allow_replay=repair_attempt > 0,
        )
        try:
            result = await _execute_task_body(spec, attempt)
        except asyncio.CancelledError:
            journal.mark_run(
                durable_run_id,
                RunLifecycleStatus.CANCELLED,
                reason=f"cancelamento durante task {spec.task_id}",
            )
            raise
        except Exception as exc:
            journal.mark_run(
                durable_run_id,
                RunLifecycleStatus.FAILED,
                reason=f"erro durante task {spec.task_id}: {type(exc).__name__}",
            )
            raise

        if result.status is AgentResultStatus.COMPLETED:
            task_status = TaskLifecycleStatus.COMPLETED
            side_effect_state = SideEffectState.COMPLETED
        elif result.status is AgentResultStatus.CANCELLED:
            task_status = TaskLifecycleStatus.CANCELLED
            side_effect_state = SideEffectState.AMBIGUOUS if not spec.read_only else SideEffectState.NONE
        elif result.status is AgentResultStatus.BLOCKED:
            task_status = TaskLifecycleStatus.BLOCKED
            side_effect_state = SideEffectState.AMBIGUOUS if not spec.read_only else SideEffectState.NONE
        else:
            retryable = any(error.retryable for error in result.errors)
            task_status = (
                TaskLifecycleStatus.FAILED_RETRYABLE
                if retryable
                else TaskLifecycleStatus.FAILED_FINAL
            )
            side_effect_state = SideEffectState.AMBIGUOUS if not spec.read_only else SideEffectState.NONE
        journal.update_task(
            durable_run_id,
            spec.task_id,
            task_status,
            attempt=attempt,
            result=result,
            side_effect_state=side_effect_state,
            artifacts=[artifact.model_dump(mode="json") for artifact in result.artifacts],
            validation_results=[validation.model_dump(mode="json") for validation in result.validation],
            errors=[error.message for error in result.errors],
        )
        return result

    completed_results: dict[str, AgentResult] = {}
    repair_targets = frozenset(task.task_id for task in plan.tasks)
    latest_review: ReviewResult | None = None
    latest_scheduler_result: SchedulerResult | None = None

    for repair_attempt in range(max_repairs + 1):
        if repair_attempt:
            await emit_workflow_state(
                event_bus,
                workflow="plan",
                status=PlanLifecycleStatus.REPAIRING.value,
                plan=plan,
            )
            if goal is not None:
                await emit_workflow_state(
                    event_bus,
                    workflow="goal",
                    status=GoalLifecycleStatus.REPAIRING.value,
                    plan=plan,
                    goal=goal,
                )
            if event_bus is not None:
                await event_bus.emit(
                    "repair.started",
                    source=AGENT_NAME,
                    payload={
                        "plan_id": plan.reference,
                        "plan_revision": plan.revision,
                        "status": "running",
                        "repair_attempt": repair_attempt,
                        "task_ids": sorted(repair_targets),
                    },
                )

        seeded_results = (
            dict(resume_results)
            if repair_attempt == 0
            else {
                task_id: result
                for task_id, result in completed_results.items()
                if task_id not in repair_targets
            }
        )
        governance = getattr(tools_runner, "governance", None)
        scheduler = DAGScheduler(
            plan.tasks,
            execute_task,
            max_concurrency=max(1, config.max_subagents),
            event_bus=event_bus,
            source=f"plan:{plan.reference}",
            initial_results=seeded_results,
            hooks=governance.hooks if governance is not None else None,
        )
        try:
            latest_scheduler_result = await scheduler.run()
            journal.record_scheduler_result(durable_run_id, latest_scheduler_result.states)
        except asyncio.CancelledError:
            journal.mark_run(
                durable_run_id,
                RunLifecycleStatus.CANCELLED,
                reason="execução cancelada pelo operador",
            )
            raise
        except Exception:
            journal.mark_run(
                durable_run_id,
                RunLifecycleStatus.FAILED,
                reason="falha do scheduler durante a execução",
            )
            await emit_workflow_state(
                event_bus,
                workflow="plan",
                status=PlanLifecycleStatus.FAILED.value,
                plan=plan,
            )
            raise
        for state in latest_scheduler_result.states:
            if state.result is not None and state.status is TaskLifecycleStatus.COMPLETED:
                completed_results[state.task_id] = state.result
                dependency_results[state.task_id] = state.result

        if repair_attempt and event_bus is not None:
            await event_bus.emit(
                "repair.completed",
                source=AGENT_NAME,
                payload={
                    "plan_id": plan.reference,
                    "plan_revision": plan.revision,
                    "status": "completed" if latest_scheduler_result.successful else "failed",
                    "repair_attempt": repair_attempt,
                },
            )

        if scoped_tools.pending_scope_expansion is not None:
            journal.mark_run(
                durable_run_id,
                RunLifecycleStatus.CANCELLED,
                reason="execução pausada para expansão de escopo",
            )
            return "Execução pausada para replanejamento e nova aprovação.", scoped_tools.pending_scope_expansion

        await emit_workflow_state(
            event_bus,
            workflow="plan",
            status=PlanLifecycleStatus.REVIEWING.value,
            plan=plan,
        )
        if spec_workflow is not None and spec_workflow.decision.use_full_workflow:
            await emit_spec_workflow_stage(
                event_bus,
                SpecWorkflowStage.REVIEW,
                status="running",
                official_artifacts=spec_workflow.decision.official_artifacts,
            )
        if goal is not None:
            await emit_workflow_state(
                event_bus,
                workflow="goal",
                status=GoalLifecycleStatus.REVIEWING.value,
                plan=plan,
                goal=goal,
            )
        try:
            latest_review = await run_independent_review(
                client=client,
                model=model,
                plan=plan,
                scheduler_result=latest_scheduler_result,
                temperature=temperature,
                api_retries=config.api_retries,
                event_bus=event_bus,
                reviewer=reviewer,
                repair_attempt=repair_attempt,
                exploration=exploration,
            )
        except Exception:
            journal.mark_run(
                durable_run_id,
                RunLifecycleStatus.FAILED,
                reason="Reviewer indisponível ou inválido",
            )
            await emit_workflow_state(
                event_bus,
                workflow="plan",
                status=PlanLifecycleStatus.FAILED.value,
                plan=plan,
            )
            raise

        failed_task_ids = [
            state.task_id
            for state in latest_scheduler_result.states
            if state.status is not TaskLifecycleStatus.COMPLETED
        ]
        if latest_review.decision is ReviewDecision.APPROVED and failed_task_ids:
            latest_review = latest_review.model_copy(
                update={
                    "decision": ReviewDecision.REJECTED,
                    "summary": (
                        "O Reviewer tentou aprovar, mas o runtime rejeitou deterministicamente porque "
                        "há tasks não concluídas."
                    ),
                    "repair_task_ids": failed_task_ids,
                }
            )
        elif latest_review.decision is ReviewDecision.REJECTED:
            missing_failed_tasks = [
                task_id for task_id in failed_task_ids if task_id not in latest_review.repair_task_ids
            ]
            if missing_failed_tasks:
                latest_review = latest_review.model_copy(
                    update={
                        "repair_task_ids": [
                            *latest_review.repair_task_ids,
                            *missing_failed_tasks,
                        ]
                    }
                )

        if event_bus is not None:
            passed = latest_review.decision is ReviewDecision.APPROVED
            await event_bus.emit(
                "review.passed" if passed else "review.failed",
                source="reviewer",
                payload={
                    "plan_id": plan.reference,
                    "plan_revision": plan.revision,
                    "status": "approved" if passed else "rejected",
                    "repair_attempt": repair_attempt,
                },
            )

        if latest_review.decision is ReviewDecision.APPROVED:
            convergence_summary = ""
            if convergence_engine is not None:
                if spec_workflow is not None:
                    await emit_spec_workflow_stage(
                        event_bus,
                        SpecWorkflowStage.CONVERGE,
                        status="running",
                        official_artifacts=spec_workflow.decision.official_artifacts,
                    )

                async def convergence_reviewer(
                    convergence_plan: Plan,
                    scheduler_result: SchedulerResult,
                    pass_number: int,
                ) -> ReviewResult:
                    return await run_independent_review(
                        client=client,
                        model=model,
                        plan=convergence_plan,
                        scheduler_result=scheduler_result,
                        temperature=temperature,
                        api_retries=config.api_retries,
                        event_bus=event_bus,
                        reviewer=reviewer,
                        repair_attempt=pass_number,
                        exploration=exploration,
                    )

                try:
                    convergence_run = await convergence_engine.run(
                        plan=plan,
                        initial_scheduler_result=latest_scheduler_result,
                        initial_review=latest_review,
                        task_executor=execute_task,
                        reviewer=convergence_reviewer,
                        analyze=analyze_result,
                        event_bus=event_bus,
                        max_concurrency=max(1, config.max_subagents),
                    )
                except Exception:
                    journal.mark_run(
                        durable_run_id,
                        RunLifecycleStatus.FAILED,
                        reason="falha durante convergence",
                    )
                    raise
                convergence_summary = (
                    f"\nConvergence: {convergence_run.final.summary} "
                    f"Passes: {len(convergence_run.history)}."
                )
                if spec_workflow is not None:
                    await emit_spec_workflow_stage(
                        event_bus,
                        SpecWorkflowStage.CONVERGE,
                        status="completed",
                        official_artifacts=spec_workflow.decision.official_artifacts,
                    )
            await emit_workflow_state(
                event_bus,
                workflow="plan",
                status=PlanLifecycleStatus.VALIDATING.value,
                plan=plan,
            )
            await emit_workflow_state(
                event_bus,
                workflow="plan",
                status=PlanLifecycleStatus.COMPLETED.value,
                plan=plan,
            )
            journal.mark_run(durable_run_id, RunLifecycleStatus.COMPLETED)
            return (
                format_execution_review(latest_scheduler_result, latest_review, repair_attempt)
                + convergence_summary,
                None,
            )

        if repair_attempt >= max_repairs:
            if event_bus is not None:
                await event_bus.emit(
                    "repair.exhausted",
                    source=AGENT_NAME,
                    payload={
                        "plan_id": plan.reference,
                        "plan_revision": plan.revision,
                        "status": "failed",
                        "repair_attempt": repair_attempt,
                        "max_repairs": max_repairs,
                    },
                )
            await emit_workflow_state(
                event_bus,
                workflow="plan",
                status=PlanLifecycleStatus.FAILED.value,
                plan=plan,
            )
            journal.mark_run(
                durable_run_id,
                RunLifecycleStatus.FAILED,
                reason="limite de repair/convergência atingido",
            )
            raise RepairLimitExceeded(latest_review, max_repairs)

        repair_targets = repair_closure(plan.tasks, latest_review.repair_task_ids)
        repair_feedback = "\n".join(
            [latest_review.summary, *latest_review.repair_instructions]
        )

    raise AssertionError("Unreachable bounded repair loop.")


async def run_goal_workflow(
    *,
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    tools_runner: WorkspaceTools,
    store: PlanStore,
    temperature: float,
    config: AgentConfig,
    objective: str,
    event_bus: EventBus | None = None,
    question_provider: Any = None,
    approval_provider: Any = None,
    revision_provider: Any = None,
    max_revisions: int = 5,
    task_executor: TaskExecutor | None = None,
    reviewer: Any = None,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
    codeintel_runtime: CodeIntelligenceRuntime | None = None,
) -> tuple[Goal, Plan, str]:
    coordinator = SpecWorkflowCoordinator(
        config.workspace,
        objective,
        inspect_workspace=config.load_project_context,
        event_bus=event_bus,
    )

    async def clarification_resolver(item: Any) -> str:
        question = UserQuestion(question_id=item.clarification_id, prompt=item.question)
        if question_provider is None:
            try:
                return read_user_input(_question_prompt(question)).strip()
            except EOFError as exc:
                raise ApprovalUnavailableError("Clarification obrigatória sem entrada disponível.") from exc
        return str(await _resolve_callback(question_provider, question)).strip()

    async def specification_writer(document: Any, updated: str) -> None:
        result = await tools_runner.execute_async(
            "write_file",
            {"path": document.relative_path, "content": updated, "overwrite": True},
            policy_approval_granted=True,
        )
        try:
            decoded = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError("write_file retornou resultado malformado durante clarify") from exc
        if not isinstance(decoded, dict) or decoded.get("error"):
            raise RuntimeError(f"Falha ao persistir clarification em {document.relative_path}")

    spec_workflow = await coordinator.prepare(
        resolver=clarification_resolver,
        writer=specification_writer,
    )
    analyze_result: AnalyzeResult | None = None
    exploration = (
        await codeintel_runtime.prepare_for_workflow(
            objective,
            for_goal=True,
            full_spec_workflow=spec_workflow.decision.use_full_workflow,
        )
        if codeintel_runtime is not None
        else None
    )

    goal = Goal(
        goal_id=f"GOAL-{time.monotonic_ns()}",
        objective=objective,
        status=GoalLifecycleStatus.PLANNING,
    )
    await emit_workflow_state(event_bus, workflow="goal", status=goal.status.value, goal=goal)
    plan = await run_plan_workflow(
        client=client,
        model=model,
        messages=messages,
        tools_runner=tools_runner,
        store=store,
        temperature=temperature,
        config=config,
        objective=objective,
        event_bus=event_bus,
        question_provider=question_provider,
        spec_workflow=spec_workflow,
        exploration=exploration,
    )
    analyze_result = await coordinator.analyze(plan)
    goal = Goal.model_validate(
        {
            **goal.model_dump(mode="python"),
            "plan_id": plan.plan_id,
            "plan_revision": plan.revision,
            "status": GoalLifecycleStatus.WAITING_APPROVAL,
            "updated_at": datetime.now(timezone.utc),
        }
    )

    revisions = 0
    while True:
        if spec_workflow.decision.use_full_workflow:
            await emit_spec_workflow_stage(
                event_bus,
                SpecWorkflowStage.HUMAN_APPROVAL,
                status="waiting",
                official_artifacts=spec_workflow.decision.official_artifacts,
            )
        await emit_workflow_state(
            event_bus,
            workflow="goal",
            status=GoalLifecycleStatus.WAITING_APPROVAL.value,
            plan=plan,
            goal=goal,
        )
        action, approval = await request_plan_approval(plan, approval_provider)
        if event_bus is not None:
            await event_bus.emit(
                "plan.approval_recorded",
                source=AGENT_NAME,
                payload={
                    "plan_id": plan.reference,
                    "plan_revision": plan.revision,
                    "status": approval.decision.value,
                },
            )
        if action == "cancel":
            goal = Goal.model_validate(
                {
                    **goal.model_dump(mode="python"),
                    "status": GoalLifecycleStatus.CANCELLED,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await emit_workflow_state(event_bus, workflow="goal", status=goal.status.value, plan=plan, goal=goal)
            return goal, plan, "Plano rejeitado; nenhuma mutação foi executada."
        if action == "revise":
            revisions += 1
            if revisions > max_revisions:
                raise ValueError(f"Limite de {max_revisions} revisões de plano atingido.")
            revision_question = UserQuestion(
                question_id=f"QUESTION-REVISION-{revisions}",
                prompt=f"Como o plano {plan.reference} deve ser revisado?",
            )
            if revision_provider is None:
                try:
                    revision_reason = read_user_input(_question_prompt(revision_question)).strip()
                except EOFError as exc:
                    raise ApprovalUnavailableError("Revisão do plano sem entrada disponível.") from exc
            else:
                revision_reason = str(await _resolve_callback(revision_provider, plan)).strip()
            if not revision_reason:
                raise ValueError("A revisão do plano precisa de orientação do operador.")
            plan = await run_plan_workflow(
                client=client,
                model=model,
                messages=messages,
                tools_runner=tools_runner,
                store=store,
                temperature=temperature,
                config=config,
                objective=f"{objective}\n\nRevisão solicitada pelo operador: {revision_reason}",
                event_bus=event_bus,
                question_provider=question_provider,
                base_plan=plan,
                revision_reason=revision_reason,
                spec_workflow=spec_workflow,
                exploration=exploration,
            )
            analyze_result = await coordinator.analyze(plan)
            goal = Goal.model_validate(
                {
                    **goal.model_dump(mode="python"),
                    "plan_revision": plan.revision,
                    "status": GoalLifecycleStatus.WAITING_APPROVAL,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            continue

        goal = Goal.model_validate(
            {
                **goal.model_dump(mode="python"),
                "status": GoalLifecycleStatus.RUNNING,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await emit_workflow_state(event_bus, workflow="goal", status=goal.status.value, plan=plan, goal=goal)
        try:
            content, scope_expansion = await run_plan_execution(
                client=client,
                model=model,
                messages=messages,
                tools_runner=tools_runner,
                store=store,
                temperature=temperature,
                config=config,
                plan=plan,
                approval=approval,
                event_bus=event_bus,
                reasoning_mode=ReasoningMode.DEEP,
                task_executor=task_executor,
                reviewer=reviewer,
                max_repairs=max_repairs,
                goal=goal,
                convergence_engine=(
                    ConvergenceEngine(max_passes=min(max_repairs, 2))
                    if spec_workflow.decision.use_full_workflow
                    else None
                ),
                analyze_result=analyze_result,
                spec_workflow=spec_workflow,
                exploration=exploration,
            )
        except (RepairLimitExceeded, ConvergenceLimitExceeded) as exc:
            goal = Goal.model_validate(
                {
                    **goal.model_dump(mode="python"),
                    "status": GoalLifecycleStatus.FAILED,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await emit_workflow_state(event_bus, workflow="goal", status=goal.status.value, plan=plan, goal=goal)
            return goal, plan, str(exc)
        except Exception:
            goal = Goal.model_validate(
                {
                    **goal.model_dump(mode="python"),
                    "status": GoalLifecycleStatus.FAILED,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await emit_workflow_state(
                event_bus,
                workflow="goal",
                status=goal.status.value,
                plan=plan,
                goal=goal,
            )
            raise
        if scope_expansion is not None:
            revisions += 1
            if revisions > max_revisions:
                raise ValueError(f"Limite de {max_revisions} revisões de plano atingido.")
            plan = await run_plan_workflow(
                client=client,
                model=model,
                messages=messages,
                tools_runner=tools_runner,
                store=store,
                temperature=temperature,
                config=config,
                objective=scope_expansion_replanning_objective(objective, plan, scope_expansion),
                event_bus=event_bus,
                question_provider=question_provider,
                base_plan=plan,
                revision_reason=f"Expansão material: {scope_expansion.reason}",
                pending_scope_expansion=scope_expansion,
                spec_workflow=spec_workflow,
                exploration=exploration,
            )
            analyze_result = await coordinator.analyze(plan)
            goal = Goal.model_validate(
                {
                    **goal.model_dump(mode="python"),
                    "plan_revision": plan.revision,
                    "status": GoalLifecycleStatus.WAITING_APPROVAL,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            continue
        goal = Goal.model_validate(
            {
                **goal.model_dump(mode="python"),
                "status": GoalLifecycleStatus.VALIDATING,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await emit_workflow_state(event_bus, workflow="goal", status=goal.status.value, plan=plan, goal=goal)
        goal = Goal.model_validate(
            {
                **goal.model_dump(mode="python"),
                "status": GoalLifecycleStatus.COMPLETED,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await emit_workflow_state(event_bus, workflow="goal", status=goal.status.value, plan=plan, goal=goal)
        return goal, plan, content


async def execute_tool_with_task_board(
    tools_runner: WorkspaceTools,
    tool_name: str,
    arguments: dict[str, Any],
    task_board: TerminalTaskBoard,
    *,
    execution_context: str = "",
) -> str:
    approval_board = getattr(tools_runner, "terminal_task_board", task_board)
    token = _TASK_BOARD_CONTEXT.set(approval_board)
    try:
        execute_async = tools_runner.execute_async
        parameters = inspect.signature(execute_async).parameters
        accepts_context = "execution_context" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts_context:
            return await execute_async(
                tool_name,
                arguments,
                execution_context=execution_context,
            )
        # Keep compatibility with small test/custom tool boundaries that
        # predate the per-turn context keyword.
        return await execute_async(tool_name, arguments)
    finally:
        _TASK_BOARD_CONTEXT.reset(token)


def latest_user_execution_context(messages: Sequence[Message]) -> str:
    """Return the current user/task request for per-command cloud resolution."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if content is not None:
            return str(content)
    return ""


async def run_agent_until_final(
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    tools_runner: WorkspaceTools,
    tool_schemas: list[dict[str, Any]],
    temperature: float,
    max_steps: int,
    api_retries: int,
    emit_tools: bool,
    event_bus: EventBus | None = None,
    agent_name: str = AGENT_NAME,
    emit_run: bool = True,
    stream_output: bool = False,
    reasoning_mode: ReasoningMode = ReasoningMode.NORMAL,
    result_protocol: AgentResultFunctionProtocol | None = None,
    result_task_id: str | None = None,
) -> str:
    started_at = time.monotonic()
    status = "failed"
    if event_bus is not None:
        if emit_run:
            await event_bus.emit(
                "run.started",
                source=agent_name,
                payload={"agent": agent_name, "status": "running"},
            )
        await event_bus.emit(
            "agent.started",
            source=agent_name,
            payload={"agent": agent_name, "status": "running"},
        )
    try:
        content = await _run_agent_until_final_impl(
            client=client,
            model=model,
            messages=messages,
            tools_runner=tools_runner,
            tool_schemas=tool_schemas,
            temperature=temperature,
            max_steps=max_steps,
            api_retries=api_retries,
            emit_tools=emit_tools,
            event_bus=event_bus,
            agent_name=agent_name,
            stream_output=stream_output,
            reasoning_mode=reasoning_mode,
            result_protocol=result_protocol,
            result_task_id=result_task_id,
        )
        governance = getattr(tools_runner, "governance", None)
        if governance is not None:
            await governance.before_final(
                agent_name,
                structured_result=result_protocol is not None,
            )
        if result_protocol is not None:
            try:
                status = AgentResult.model_validate_json(content).status.value
            except ValidationError:
                status = "failed"
        else:
            status = (
                "incomplete"
                if content.startswith("Limite de ") or content == "A API retornou sem choices."
                else "completed"
            )
        return content
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        if event_bus is not None:
            duration = time.monotonic() - started_at
            await event_bus.emit(
                "agent.completed",
                source=agent_name,
                payload={
                    "agent": agent_name,
                    "status": status,
                    "duration_seconds": duration,
                },
            )
            if emit_run:
                await event_bus.emit(
                    "run.completed",
                    source=agent_name,
                    payload={
                        "agent": agent_name,
                        "status": status,
                        "duration_seconds": duration,
                    },
                )


async def _run_agent_until_final_impl(
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    tools_runner: WorkspaceTools,
    tool_schemas: list[dict[str, Any]],
    temperature: float,
    max_steps: int,
    api_retries: int,
    emit_tools: bool,
    event_bus: EventBus | None,
    agent_name: str,
    stream_output: bool,
    reasoning_mode: ReasoningMode,
    result_protocol: AgentResultFunctionProtocol | None,
    result_task_id: str | None,
) -> str:
    step_budget = min(INITIAL_STEP_BUDGET, max_steps)
    task_board = TerminalTaskBoard(
        max_slots=step_budget,
        enabled=emit_tools and event_bus is None,
        max_visible_tasks=DEFAULT_MAX_VISIBLE_TASKS,
    )
    compaction_reported = False
    invalid_result_attempts = 0
    force_result_submission = False
    protocol_reserve = result_protocol.max_repairs + 1 if result_protocol is not None else 0
    iteration_limit = max_steps + protocol_reserve
    for step in range(1, iteration_limit + 1):
        if result_protocol is not None and step > max_steps:
            force_result_submission = True
        try:
            context_started_at = time.monotonic()
            active_context_engine = getattr(tools_runner, "_context_engine", None)
            if active_context_engine is not None:
                sync_context_state_message(messages, active_context_engine)
            context_budget = ContextBudget(getattr(client, "capabilities", None))
            prepared_context = prepare_token_bounded_messages(
                messages,
                context_budget,
                tools=tool_schemas,
            )
            request_messages = prepared_context.messages
            omitted_messages = prepared_context.omitted_messages
            if event_bus is not None:
                await event_bus.emit(
                    "context.prepared",
                    source=agent_name,
                    payload=context_budget_payload(
                        prepared_context.budget,
                        duration_seconds=time.monotonic() - context_started_at,
                        omitted_messages=omitted_messages,
                    ),
                )
                if omitted_messages:
                    await event_bus.emit(
                        "context.compacted",
                        source=agent_name,
                        payload={"omitted_messages": omitted_messages, "status": "compacted"},
                    )
            if omitted_messages and emit_tools and not compaction_reported:
                print_labeled(
                    "Contexto>",
                    f"{omitted_messages} mensagens antigas foram omitidas desta chamada para evitar excesso de contexto.",
                    style="yellow",
                    content_style="yellow",
                )
                compaction_reported = True
            if event_bus is not None:
                await event_bus.emit(
                    "agent.waiting_model",
                    source=agent_name,
                    payload={
                        "agent": agent_name,
                        "model": model,
                        "status": "waiting_model",
                    },
                )
            response = await create_chat_completion_with_retry(
                client,
                operation="agent_turn",
                api_retries=api_retries,
                emit_status=emit_tools,
                loading_enabled=emit_tools and not task_board.tasks,
                request=ModelRequest(
                    messages=request_messages,
                    tools=tool_schemas,
                    tool_choice=(
                        {"type": "function", "function": {"name": SUBMIT_AGENT_RESULT_FUNCTION}}
                        if force_result_submission
                        else "auto"
                    ),
                    temperature=temperature,
                    reasoning_mode=reasoning_mode,
                ),
                event_bus=event_bus,
                agent_name=agent_name,
                visible=stream_output,
                use_stream=event_bus is not None and client.capabilities.supports("streaming"),
                context_budget=context_budget,
            )
            if active_context_engine is not None:
                active_context_engine.record_usage(response.usage)
        except (OpenAIError, KeyboardInterrupt, PromptTooLargeError):
            task_board.finish(failed=True)
            raise

        if not response.content and not response.tool_calls and result_protocol is None:
            task_board.finish(failed=True)
            return "A API retornou sem choices."

        tool_calls = list(response.tool_calls)

        if not tool_calls:
            content = response.content
            messages.append({"role": "assistant", "content": content})
            if result_protocol is not None:
                invalid_result_attempts += 1
                if invalid_result_attempts <= result_protocol.max_repairs:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Resultado final inválido: use obrigatoriamente a function "
                                f"{SUBMIT_AGENT_RESULT_FUNCTION} e envie arguments conformes a AgentResult. "
                                "Faça somente este repair final."
                            ),
                        }
                    )
                    force_result_submission = True
                    continue
                failed = AgentResult(
                    task_id=result_task_id or "TASK-UNKNOWN",
                    status=AgentResultStatus.FAILED,
                    summary="O subagente não produziu um AgentResult válido.",
                    errors=[
                        AgentError(
                            code=AgentErrorCode.PROTOCOL_ERROR,
                            message="Repair de AgentResult esgotado: function call ausente.",
                            details={"attempts": invalid_result_attempts, "protocol_code": "repair_exhausted"},
                        )
                    ],
                )
                task_board.finish(failed=True)
                return failed.model_dump_json()
            task_board.finish()
            return content

        if step <= max_steps and step >= step_budget and step_budget < max_steps:
            step_budget = min(step_budget + STEP_BUDGET_INCREMENT, max_steps)
            task_board.expand_slots(step_budget)

        serialized_tool_calls = [serialize_tool_call(tool_call) for tool_call in tool_calls]
        messages.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": serialized_tool_calls,
            }
        )

        if result_protocol is not None and any(
            tool_call.name == SUBMIT_AGENT_RESULT_FUNCTION for tool_call in tool_calls
        ):
            invalid_result_attempts += 1
            result_tool_call = next(
                tool_call for tool_call in tool_calls if tool_call.name == SUBMIT_AGENT_RESULT_FUNCTION
            )
            result_call = (
                FunctionCall(name=result_tool_call.name, arguments=result_tool_call.arguments)
                if len(tool_calls) == 1
                else FunctionCall(name="mixed_agent_result_calls", arguments={})
            )
            protocol_error: AgentResultProtocolError | None = None
            try:
                agent_result = result_protocol.validate(result_call, attempt=invalid_result_attempts)
            except AgentResultProtocolError as exc:
                protocol_error = exc
            else:
                if result_task_id is not None and agent_result.task_id != result_task_id:
                    protocol_error = AgentResultProtocolError(
                        ResultProtocolFailure(
                            code=ResultProtocolErrorCode.INVALID_ARGUMENTS,
                            message=f"AgentResult.task_id deve ser {result_task_id!r}.",
                            attempts=invalid_result_attempts,
                        )
                    )

            if protocol_error is None:
                task_board.finish()
                return agent_result.model_dump_json()

            repair_allowed = invalid_result_attempts <= result_protocol.max_repairs
            for tool_call in tool_calls:
                is_result_call = tool_call.name == SUBMIT_AGENT_RESULT_FUNCTION
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": to_json(
                            {
                                "error": (
                                    protocol_error.failure.code.value
                                    if is_result_call
                                    else "mixed_with_agent_result"
                                ),
                                "message": (
                                    protocol_error.failure.message
                                    if is_result_call
                                    else "Tool não executada: submit_agent_result deve ser a única call final."
                                ),
                                "repair_allowed": repair_allowed,
                            }
                        ),
                    }
                )
            if repair_allowed:
                force_result_submission = True
                continue
            failed = AgentResult(
                task_id=result_task_id or "TASK-UNKNOWN",
                status=AgentResultStatus.FAILED,
                summary="O subagente não produziu um AgentResult válido.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.PROTOCOL_ERROR,
                        message=f"Repair de AgentResult esgotado: {protocol_error.failure.message}",
                        details={
                            "attempts": invalid_result_attempts,
                            "protocol_code": "repair_exhausted",
                        },
                    )
                ],
            )
            task_board.finish(failed=True)
            return failed.model_dump_json()

        if result_protocol is not None and step > max_steps:
            invalid_result_attempts += 1
            repair_allowed = invalid_result_attempts <= result_protocol.max_repairs
            for tool_call in tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": to_json(
                            {
                                "error": "final_result_required",
                                "message": (
                                    f"O orçamento operacional terminou. Envie somente "
                                    f"{SUBMIT_AGENT_RESULT_FUNCTION}."
                                ),
                                "repair_allowed": repair_allowed,
                            }
                        ),
                    }
                )
            if repair_allowed:
                force_result_submission = True
                continue
            failed = AgentResult(
                task_id=result_task_id or "TASK-UNKNOWN",
                status=AgentResultStatus.FAILED,
                summary="O subagente não produziu um AgentResult válido.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.PROTOCOL_ERROR,
                        message=f"{SUBMIT_AGENT_RESULT_FUNCTION} não foi enviado no fechamento reservado.",
                        details={"attempts": invalid_result_attempts, "protocol_code": "repair_exhausted"},
                    )
                ],
            )
            task_board.finish(failed=True)
            return failed.model_dump_json()

        prepared_tool_calls: list[tuple[Any, str, dict[str, Any], str | None]] = []
        for tool_call in tool_calls:
            tool_name = tool_call.name
            arguments: dict[str, Any] = {}
            try:
                arguments = parse_tool_arguments(tool_call.arguments)
            except json.JSONDecodeError as exc:
                prepared_tool_calls.append(
                    (tool_call, tool_name, arguments, to_json({"error": "JSONDecodeError", "message": str(exc)}))
                )
                continue
            except ValueError as exc:
                prepared_tool_calls.append(
                    (tool_call, tool_name, arguments, to_json({"error": "ValueError", "message": str(exc)}))
                )
                continue
            prepared_tool_calls.append((tool_call, tool_name, arguments, None))

        batch = task_board.add_batch(
            [(tool_name, arguments, step, step_budget) for _, tool_name, arguments, _ in prepared_tool_calls]
        )
        task_board.start_batch(batch)

        results: list[str | None] = [None] * len(prepared_tool_calls)
        parallel_requests = [
            (tool_name, arguments)
            for _, tool_name, arguments, parse_error_result in prepared_tool_calls
            if parse_error_result is None
        ]
        run_tools_in_parallel = (
            len(parallel_requests) == len(prepared_tool_calls)
            and can_parallelize_tools(parallel_requests)
        )

        try:
            if run_tools_in_parallel:
                semaphore = asyncio.Semaphore(
                    min(DEFAULT_MAX_PARALLEL_TOOLS, len(prepared_tool_calls))
                )

                async def execute_parallel(index: int) -> str:
                    async with semaphore:
                        _, tool_name, arguments, _ = prepared_tool_calls[index]
                        return await execute_tool_with_task_board(
                            tools_runner,
                            tool_name,
                            arguments,
                            task_board,
                            execution_context=latest_user_execution_context(messages),
                        )

                tasks = [
                    asyncio.create_task(execute_parallel(index))
                    for index in range(len(prepared_tool_calls))
                ]
                try:
                    parallel_results = await asyncio.gather(*tasks)
                except BaseException:
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                for index, result in enumerate(parallel_results):
                    results[index] = result
                    if emit_tools:
                        task_board.complete_task(batch[index], prepared_tool_calls[index][1], result)
            else:
                for index, (task, (_, tool_name, arguments, parse_error_result)) in enumerate(
                    zip(batch, prepared_tool_calls)
                ):
                    if parse_error_result is not None:
                        result = parse_error_result
                    else:
                        result = await execute_tool_with_task_board(
                            tools_runner,
                            tool_name,
                            arguments,
                            task_board,
                            execution_context=latest_user_execution_context(messages),
                        )
                    results[index] = result
                    if emit_tools:
                        task_board.complete_task(task, tool_name, result)
        except (ApprovalUnavailableError, KeyboardInterrupt):
            task_board.finish(failed=True)
            raise

        for result, (tool_call, tool_name, _, _) in zip(results, prepared_tool_calls):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": truncate_text(result or ""),
                }
            )

    task_board.finish(failed=True)
    if result_protocol is not None:
        return AgentResult(
            task_id=result_task_id or "TASK-UNKNOWN",
            status=AgentResultStatus.FAILED,
            summary="O subagente atingiu o limite antes de enviar um AgentResult válido.",
            errors=[
                AgentError(
                    code=AgentErrorCode.PROTOCOL_ERROR,
                    message=f"{SUBMIT_AGENT_RESULT_FUNCTION} ausente ou inválido até o limite de passos.",
                    details={"attempts": invalid_result_attempts, "protocol_code": "repair_exhausted"},
                )
            ],
        ).model_dump_json()
    return f"Limite de {max_steps} passos atingido. Peça para continuar se necessário."


async def run_agent_turn(
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    tools_runner: WorkspaceTools,
    tool_schemas: list[dict[str, Any]],
    temperature: float,
    max_steps: int,
    api_retries: int,
    event_bus: EventBus | None = None,
    reasoning_mode: ReasoningMode = ReasoningMode.NORMAL,
) -> None:
    content = await run_agent_until_final(
        client=client,
        model=model,
        messages=messages,
        tools_runner=tools_runner,
        tool_schemas=tool_schemas,
        temperature=temperature,
        max_steps=max_steps,
        api_retries=api_retries,
        emit_tools=True,
        event_bus=event_bus,
        stream_output=event_bus is not None,
        reasoning_mode=reasoning_mode,
    )
    if content.startswith("Limite de "):
        print(f"{YELLOW}{content}{RESET}")
        return
    if content == "A API retornou sem choices.":
        print(f"{RED}{content}{RESET}", file=sys.stderr)
        return
    if event_bus is None:
        print_assistant(content)


async def persist_history_on_exit(
    client: ModelAdapter,
    model: str,
    messages: list[Message],
    config: AgentConfig,
    last_saved_digest: str | None,
    reason: str,
    prefer_model_summary: bool,
) -> str | None:
    try:
        save_result = await save_history_if_changed(
            client=client,
            model=model,
            messages=messages,
            config=config,
            last_saved_digest=last_saved_digest,
            save_reason=reason,
            prefer_model_summary=prefer_model_summary,
        )
    except KeyboardInterrupt:
        print()
        try:
            save_result = await save_history_if_changed(
                client=client,
                model=model,
                messages=messages,
                config=config,
                last_saved_digest=last_saved_digest,
                save_reason=reason,
                prefer_model_summary=False,
            )
        except (OSError, PermissionError, ValueError) as exc:
            LOGGER.error("automatic_history_failed reason=%s error=%s", reason, exception_chain_summary(exc))
            print_styled(f"Falha ao salvar histórico automático: {exc}", style="red", file=sys.stderr)
            return last_saved_digest
    except (OSError, PermissionError, ValueError) as exc:
        LOGGER.error("automatic_history_failed reason=%s error=%s", reason, exception_chain_summary(exc))
        print_styled(f"Falha ao salvar histórico automático: {exc}", style="red", file=sys.stderr)
        return last_saved_digest

    if save_result is None:
        return last_saved_digest
    print_labeled(
        "Histórico>",
        f"salvo automaticamente em {save_result.path}",
        style="green",
        content_style="green",
    )
    if save_result.fallback_reason:
        print_labeled(
            "Histórico>",
            "resumo via modelo indisponível; foi salvo fallback local sanitizado.",
            style="yellow",
            content_style="yellow",
        )
    return save_result.transcript_digest


async def agent_loop(
    client: ModelAdapter,
    model: str,
    model_resolution: str,
    config: AgentConfig,
    temperature: float,
    agent_profiles: dict[str, AgentProfile],
    event_bus: EventBus | None = None,
    operational_state: OperationalState | None = None,
    metrics: LocalMetricsCollector | None = None,
    codeintel_runtime: CodeIntelligenceRuntime | None = None,
    terminal_ui: TerminalUI | None = None,
) -> None:
    operational_state = operational_state or OperationalState()
    metrics = metrics or LocalMetricsCollector()
    profile_names = sorted(agent_profiles)
    context_warnings: list[str] = []
    try:
        skill_registry = build_local_skill_registry(config)
    except SkillRegistryError as exc:
        skill_registry = None
        context_warnings.append(f"Skills locais indisponíveis: {exc}")
    try:
        spec_context = detect_spec_kit_context(config)
    except SpecKitError as exc:
        spec_context = None
        context_warnings.append(f"Spec Kit indisponível: {exc}")
    context_engine = ContextEngine(config.workspace)
    codeintel_runtime = codeintel_runtime or CodeIntelligenceRuntime(
        config.workspace,
        artifact_store=context_engine.artifact_store,
        event_bus=event_bus,
        model_client=client,
        model=model,
        temperature=temperature,
    )
    checkpoint_manager = CheckpointManager(config.workspace)
    mcp_registry = MCPRegistry(event_bus=event_bus, artifact_store=context_engine.artifact_store)
    herdr_backend = HerdrTerminalBackend(probe_cwd=Path(config.workspace))
    governance = RuntimeGovernance.for_workspace(
        config.workspace,
        constitution_inputs=tuple(
            document.content
            for document in (spec_context.documents if spec_context is not None else ())
            if document.category is SpecKitCategory.CONSTITUTION
        )
    )
    tools_runner = WorkspaceTools(
        config,
        client=client,
        model=model,
        temperature=temperature,
        agent_profiles=agent_profiles,
        event_bus=event_bus,
        artifact_store=context_engine.artifact_store,
        context_engine=context_engine,
        governance=governance,
        post_write_callback=codeintel_runtime.sync_written_file,
    )
    if terminal_ui is not None:
        tools_runner.terminal_task_board = terminal_ui.task_board
    tool_schemas = build_tool_schemas(
        allow_shell=config.allow_shell,
        allow_write=True,
        allow_subagents=config.max_subagents > 0,
        subagent_max_steps=config.subagent_max_steps,
        profile_names=profile_names,
    )
    messages: list[Message] = create_initial_messages(
        config,
        agent_profiles,
        skill_registry=skill_registry,
        spec_context=spec_context,
    )
    sync_context_state_message(messages, context_engine)
    conversation_mode = "chat"
    main_reasoning_mode = ReasoningMode.DEEP
    prompt_session = build_prompt_session()
    last_saved_digest: str | None = None
    api_available = True

    print_labeled(
        "AgenteGlobal>",
        f"perfil {config.user_profile_id} | Deep on | use /help",
        style="cyan",
        content_style="gray",
    )
    for warning in context_warnings:
        print_labeled("Contexto>", warning, style="yellow", content_style="yellow")

    while True:
        try:
            if terminal_ui is not None:
                terminal_ui.prepare_for_input()
            user_input = (
                (
                    await read_user_input_async(
                        f"{inline_styled('Você>', 'cyan')} ",
                        prompt_session=prompt_session,
                    )
                )
                .replace("\ufeff", "")
                .replace("ï»¿", "")
                .strip()
            )
        except EOFError:
            print()
            last_saved_digest = await persist_history_on_exit(
                client, model, messages, config, last_saved_digest, "eof", api_available
            )
            return
        except KeyboardInterrupt:
            print()
            last_saved_digest = await persist_history_on_exit(
                client, model, messages, config, last_saved_digest, "keyboard_interrupt", False
            )
            return

        if not user_input:
            continue
        if terminal_ui is not None:
            terminal_ui.begin_turn()
        if len(user_input) > MAX_USER_INPUT_CHARS:
            try:
                user_input, large_input = ingest_oversized_user_input(user_input, context_engine)
            except (OSError, ValueError) as exc:
                print_labeled("Entrada>", f"falha na ingestão local: {exc}", style="red", content_style="red")
                continue
            sync_context_state_message(messages, context_engine)
            print_labeled(
                "Entrada>",
                f"preservada em {large_input.artifact_id}; {large_input.ingestion.chunk_count} chunks indexados.",
                style="green",
                content_style="green",
            )
            if event_bus is not None:
                await event_bus.emit(
                    "artifact.created",
                    source=AGENT_NAME,
                    payload={"status": "created", "chunk_count": large_input.ingestion.chunk_count},
                )
                await event_bus.emit(
                    "retrieval.completed",
                    source=AGENT_NAME,
                    payload={
                        "hit_count": len(large_input.retrieval.hits),
                        "status": "hit" if large_input.retrieval.hits else "miss",
                    },
                )

        command = user_input.lower()
        if is_incomplete_slash_command(command):
            print_slash_suggestions(command)
            continue
        if command in {"/exit", "/quit", "/sair", "/q"}:
            last_saved_digest = await persist_history_on_exit(
                client, model, messages, config, last_saved_digest, "exit", api_available
            )
            return
        if command in {"/help", "/ajuda"}:
            print_help(config)
            continue
        if command == "/mode" or command.startswith("/mode "):
            mode_value = user_input[5:].strip()
            if not mode_value:
                print_permission_mode(config)
                continue
            try:
                permission_mode = normalize_permission_mode(mode_value)
            except ValueError as exc:
                print(f"{RED}{exc}{RESET}")
                continue
            config = replace(config, permission_mode=permission_mode)
            tools_runner.config = config
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"Modo de permissão alterado para {permission_mode}: "
                        f"{PERMISSION_MODES[permission_mode]}."
                    ),
                }
            )
            print_permission_mode(config)
            continue
        if command == "/deep" or command.startswith("/deep "):
            deep_value = user_input[5:].strip()
            try:
                main_reasoning_mode, changed = resolve_deep_command(
                    deep_value,
                    main_reasoning_mode,
                )
            except ValueError as exc:
                print(f"{RED}{exc}{RESET}")
                continue
            if changed:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Deep Thinking do agente principal alterado para "
                            f"{'on' if main_reasoning_mode is ReasoningMode.DEEP else 'off'}. "
                            "Use explicitamente o reasoning correspondente; não dependa do default do provider."
                        ),
                    }
                )
            print_deep_mode(main_reasoning_mode)
            continue
        if command == "/explore" or command.startswith("/explore "):
            objective = user_input[8:].strip()
            if not objective:
                print(f"{YELLOW}Uso: /explore <objetivo ou escopo>{RESET}")
                continue
            try:
                print_labeled("Explore>", "indexando e verificando evidências read-only...", style="cyan")
                exploration_run = await codeintel_runtime.explore(
                    objective,
                    depth=ExplorationDepth.DEEP,
                    reuse=True,
                    synthesize=True,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                print_labeled("Explore>", str(exc), style="red", content_style="red")
                continue
            terminal_report = render_exploration_report(exploration_run.report)
            content = (
                f"{exploration_run.synthesis}\n\n{terminal_report}"
                if exploration_run.synthesis
                else terminal_report
            )
            artifact_summary = (
                f"\n\nArtifacts: report={exploration_run.artifacts.exploration_report}; "
                f"graph={exploration_run.artifacts.architecture_graph}; "
                f"flow={exploration_run.artifacts.execution_flow}; "
                f"symbols={exploration_run.artifacts.symbol_snapshot}."
            )
            if exploration_run.synthesis_error:
                artifact_summary += f"\nSíntese do modelo indisponível: {exploration_run.synthesis_error}"
            content += artifact_summary
            messages.append({"role": "user", "content": user_input})
            messages.append({"role": "assistant", "content": content})
            print_assistant(content)
            continue
        verbosity_prefixes = ("/verbosity", "/verbosidade", "/verbose")
        if command in verbosity_prefixes or any(command.startswith(f"{prefix} ") for prefix in verbosity_prefixes):
            parts = user_input.split(maxsplit=1)
            verbosity_value = parts[1].strip() if len(parts) > 1 else ""
            if not verbosity_value:
                print_verbosity_mode(config)
                continue
            try:
                verbosity_mode = normalize_verbosity_mode(verbosity_value)
            except ValueError as exc:
                print(f"{RED}{exc}{RESET}")
                continue
            config = replace(config, verbosity_mode=verbosity_mode)
            tools_runner.config = config
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"Modo de verbosidade alterado para {verbosity_mode}: "
                        f"{VERBOSITY_MODES[verbosity_mode]}. "
                        f"Regra efetiva: {verbosity_style_instruction(verbosity_mode)}"
                    ),
                }
            )
            print_verbosity_mode(config)
            continue
        if command == "/plan" or command.startswith("/plan "):
            plan_body = user_input[5:].strip()
            if plan_body.lower() in {"off", "clear", "exit", "chat", "default"}:
                conversation_mode = "chat"
                messages.append({"role": "system", "content": "Modo /plan desativado. Volte ao modo padrão."})
                print(f"{CYAN}Modo>{RESET} chat")
                continue
            conversation_mode = "plan"
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Modo /plan ativo. Cada pedido será investigado em Deep Thinking, "
                        "sem mutações, e persistido com PLAN-ID."
                    ),
                }
            )
            if not plan_body:
                print(f"{CYAN}Modo>{RESET} plan")
                continue

            turn_start = len(messages)
            tools_runner.subagents_started = 0
            try:
                exploration = await codeintel_runtime.prepare_for_workflow(
                    plan_body,
                    for_goal=False,
                )
                plan = await run_plan_workflow(
                    client=client,
                    model=model,
                    messages=messages,
                    tools_runner=tools_runner,
                    store=PlanStore(config.workspace),
                    temperature=temperature,
                    config=config,
                    objective=plan_body,
                    event_bus=event_bus,
                    exploration=exploration,
                )
            except OpenAIError as exc:
                api_available = False
                append_api_failure_context(messages, turn_start, exc)
                report_api_error(exc)
            except PromptTooLargeError as exc:
                close_oversized_turn(messages, turn_start, exc)
                print_labeled("Contexto>", str(exc), style="red", content_style="red")
            except (ApprovalUnavailableError, PlanStoreError, PermissionError, ValueError) as exc:
                print_labeled("Plano>", str(exc), style="red", content_style="red")
            except KeyboardInterrupt:
                print()
                last_saved_digest = await persist_history_on_exit(
                    client, model, messages, config, last_saved_digest, "keyboard_interrupt", False
                )
                return
            else:
                api_available = True
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": format_plan_summary(plan)})
                print_assistant(format_plan_summary(plan))
            continue
        if command == "/chat" or command in {"/default", "/modo chat"}:
            conversation_mode = "chat"
            messages.append({"role": "system", "content": "Modo padrão de chat ativo."})
            print(f"{CYAN}Modo>{RESET} chat")
            continue
        if command == "/run" or command.startswith("/run "):
            plan_reference = user_input[4:].strip()
            if not plan_reference:
                print(f"{YELLOW}Uso: /run PLAN-ID{RESET}")
                continue
            turn_start = len(messages)
            tools_runner.subagents_started = 0
            try:
                store = PlanStore(config.workspace)
                plan = store.load(plan_reference)
                approval = create_plan_approval(
                    plan,
                    decision=ApprovalDecision.APPROVED,
                    comment="Execução explicitamente solicitada por /run.",
                    decided_by="operador via /run",
                )
                if event_bus is not None:
                    await event_bus.emit(
                        "plan.approval_recorded",
                        source=AGENT_NAME,
                        payload={
                            "plan_id": plan.reference,
                            "plan_revision": plan.revision,
                            "status": approval.decision.value,
                        },
                    )
                content, scope_expansion = await run_plan_execution(
                    client=client,
                    model=model,
                    messages=messages,
                    tools_runner=tools_runner,
                    store=store,
                    temperature=temperature,
                    config=config,
                    plan=plan,
                    approval=approval,
                    event_bus=event_bus,
                    reasoning_mode=main_reasoning_mode,
                )
                revised_plan = None
                if scope_expansion is not None:
                    revised_plan = await run_plan_workflow(
                        client=client,
                        model=model,
                        messages=messages,
                        tools_runner=tools_runner,
                        store=store,
                        temperature=temperature,
                        config=config,
                        objective=scope_expansion_replanning_objective(
                            plan.objective,
                            plan,
                            scope_expansion,
                        ),
                        event_bus=event_bus,
                        base_plan=plan,
                        revision_reason=f"Expansão material: {scope_expansion.reason}",
                        pending_scope_expansion=scope_expansion,
                    )
            except (PlanNotFoundError, PlanStoreError, PermissionError, ValueError) as exc:
                print_labeled("Run>", str(exc), style="red", content_style="red")
                continue
            except OpenAIError as exc:
                api_available = False
                append_api_failure_context(messages, turn_start, exc)
                report_api_error(exc)
                continue
            if revised_plan is not None:
                print_labeled(
                    "Run>",
                    f"pausado por expansão material. O plano foi refeito em Deep Thinking; "
                    f"revise e aprove explicitamente com /run {revised_plan.reference}.",
                    style="yellow",
                    content_style="yellow",
                )
            else:
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": content})
                if event_bus is None:
                    print_assistant(content)
            api_available = True
            continue
        if command == "/resume" or command.startswith("/resume "):
            run_id = user_input[7:].strip()
            if not run_id:
                print(f"{YELLOW}Uso: /resume RUN-ID{RESET}")
                continue
            turn_start = len(messages)
            tools_runner.subagents_started = 0
            try:
                store = PlanStore(config.workspace)
                journal = RunJournal(config.workspace)
                durable_run = journal.load(run_id)
                plan = store.load(f"{durable_run.plan_id}-r{durable_run.plan_revision}")
                approval = create_plan_approval(
                    plan,
                    decision=ApprovalDecision.APPROVED,
                    comment="Resume explicitamente solicitado pelo operador via /resume.",
                    decided_by="operador via /resume",
                )
                if event_bus is not None:
                    await event_bus.emit(
                        "plan.approval_recorded",
                        source=AGENT_NAME,
                        payload={
                            "plan_id": plan.reference,
                            "plan_revision": plan.revision,
                            "status": approval.decision.value,
                            "reason": "resume",
                        },
                    )
                content, scope_expansion = await run_plan_execution(
                    client=client,
                    model=model,
                    messages=messages,
                    tools_runner=tools_runner,
                    store=store,
                    temperature=temperature,
                    config=config,
                    plan=plan,
                    approval=approval,
                    event_bus=event_bus,
                    reasoning_mode=main_reasoning_mode,
                    run_id=run_id,
                    run_journal=journal,
                )
            except (RunNotFoundError, RunCorruptError, RunPlanMismatchError, PlanNotFoundError, PlanStoreError, PermissionError, ValueError) as exc:
                print_labeled("Resume>", str(exc), style="red", content_style="red")
                continue
            except OpenAIError as exc:
                api_available = False
                append_api_failure_context(messages, turn_start, exc)
                report_api_error(exc)
                continue
            if scope_expansion is not None:
                print_labeled(
                    "Resume>",
                    "pausado por expansão material; o plano exige nova aprovação explícita.",
                    style="yellow",
                    content_style="yellow",
                )
            else:
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": content})
                if event_bus is None:
                    print_assistant(content)
            api_available = True
            continue
        spawn_prefixes = ("/spawn", "/subagent", "/subagente")
        if command in spawn_prefixes or any(command.startswith(f"{prefix} ") for prefix in spawn_prefixes):
            parts = user_input.split(maxsplit=1)
            spawn_body = parts[1].strip() if len(parts) > 1 else ""
            spawn_body, allow_mutation, requested_profile = parse_manual_spawn_options(spawn_body)
            if not spawn_body:
                print(f"{YELLOW}Uso: /spawn [--profile nome] [--read-only] <tarefa objetiva>{RESET}")
                continue
            if conversation_mode == "plan" and allow_mutation:
                print(
                    f"{YELLOW}Modo /plan não permite subagente com mutação. "
                    f"Use /spawn --read-only <tarefa> ou volte com /chat.{RESET}"
                )
                continue

            tools_runner.subagents_started = 0
            try:
                result = await tools_runner.execute_async(
                    "spawn_subagent",
                    {
                        "task": spawn_body,
                        "name": "manual",
                        "scope": "Invocação explícita pelo operador.",
                        "allow_mutation": allow_mutation,
                        "profile": requested_profile,
                    },
                    runtime_mutation_grant=allow_mutation,
                )
            except ApprovalUnavailableError as exc:
                print_labeled("Subagente>", str(exc), style="red", content_style="red")
                continue
            except KeyboardInterrupt:
                print()
                last_saved_digest = await persist_history_on_exit(
                    client, model, messages, config, last_saved_digest, "keyboard_interrupt", False
                )
                return
            try:
                parsed_result = json.loads(result)
            except json.JSONDecodeError:
                parsed_result = {"error": "InvalidSubagentResult", "message": result}

            messages.append({"role": "user", "content": user_input})
            if parsed_result.get("error"):
                error_message = str(parsed_result.get("message") or parsed_result["error"])
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"A invocação manual do subagente falhou: {error_message}",
                    }
                )
                if parsed_result.get("api_error"):
                    api_available = False
                if terminal_ui is not None:
                    terminal_ui.task_board.finish(failed=True)
                print_labeled("Subagente>", error_message, style="red", content_style="red")
                continue

            subagent_name = str(parsed_result.get("subagent") or "manual")
            subagent_status = str(parsed_result.get("status") or "incomplete")
            subagent_answer = str(parsed_result.get("answer") or "Sem resposta.")
            result_style = "green" if subagent_status == "completed" else "yellow"
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Resultado do subagente {subagent_name} ({subagent_status}):\n{subagent_answer}",
                }
            )
            api_available = True
            if terminal_ui is not None:
                terminal_ui.task_board.finish(failed=subagent_status == "failed")
            print_labeled(
                f"Subagente {subagent_name}>",
                subagent_answer,
                style=result_style,
                content_style=result_style,
            )
            continue
        if command == "/goal" or command.startswith("/goal "):
            goal_body = user_input[5:].strip()
            if goal_body.lower() in {"clear", "cancel", "cancelar"}:
                print(f"{CYAN}Goal>{RESET} nenhum loop persistente ativo; a execução de /goal é síncrona nesta versão.")
                continue
            try:
                max_iterations, objective = parse_goal_command(goal_body)
            except ValueError as exc:
                print(f"{RED}{exc}{RESET}")
                continue

            turn_start = len(messages)
            try:
                goal, plan, content = await run_goal_workflow(
                    client=client,
                    model=model,
                    messages=messages,
                    tools_runner=tools_runner,
                    store=PlanStore(config.workspace),
                    temperature=temperature,
                    config=config,
                    objective=objective,
                    event_bus=event_bus,
                    max_revisions=max_iterations,
                    codeintel_runtime=codeintel_runtime,
                )
            except OpenAIError as exc:
                api_available = False
                append_api_failure_context(messages, turn_start, exc)
                report_api_error(exc)
            except KeyboardInterrupt:
                print()
                last_saved_digest = await persist_history_on_exit(
                    client, model, messages, config, last_saved_digest, "keyboard_interrupt", False
                )
                return
            except PromptTooLargeError as exc:
                close_oversized_turn(messages, turn_start, exc)
                print_labeled("Contexto>", str(exc), style="red", content_style="red")
            except (ApprovalUnavailableError, PlanStoreError, PermissionError, ValueError) as exc:
                del messages[turn_start:]
                print_labeled("Goal>", str(exc), style="red", content_style="red")
            else:
                api_available = True
                messages.append({"role": "user", "content": user_input})
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"Goal {goal.goal_id} ({goal.status.value}), plano {plan.reference}:\n{content}",
                    }
                )
                if goal.status is GoalLifecycleStatus.CANCELLED or event_bus is None:
                    print_assistant(content)
            continue
        if command == "/skills" or command.startswith("/skills "):
            if skill_registry is None:
                print_labeled("Skills>", "registry local indisponível.", style="yellow", content_style="yellow")
                continue
            skill_args = user_input[7:].strip()
            try:
                if not skill_args:
                    print(render_skills(skill_registry.list_metadata(), operational_state.snapshot()["skills"]))
                    continue
                elif skill_args.lower().startswith("search "):
                    output = "\n".join(
                        ["Skills"]
                        + [
                            f"  {item.name}: source={item.origin}"
                            for item in skill_registry.search(skill_args[7:].strip())
                        ]
                    )
                elif skill_args.lower().startswith("resources "):
                    output = [item.to_dict() for item in skill_registry.list_resources(skill_args[10:].strip())]
                else:
                    skill_name = skill_args[8:].strip() if skill_args.lower().startswith("inspect ") else skill_args
                    document = skill_registry.load(skill_name)
                    token_footprint = estimate_skill_tokens(document.content)
                    if event_bus is not None:
                        await event_bus.emit(
                            "skill.loaded",
                            source=AGENT_NAME,
                            payload={
                                "status": "loaded",
                                "skill_name": document.metadata.name,
                                "source": document.metadata.origin,
                                "version": document.metadata.version,
                                "trust": document.metadata.trust,
                                "token_footprint": token_footprint,
                                "disclosure_level": "L1",
                            },
                        )
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                f"<skill_context level=\"1\" name=\"{document.metadata.name}\">\n"
                                f"{document.content}\n</skill_context>"
                            ),
                        }
                    )
                    output = f"Skill {document.metadata.name} carregada (source={document.metadata.origin})."
                print(output if isinstance(output, str) else to_json(output))
            except (SkillRegistryError, ValueError) as exc:
                print_labeled("Skills>", str(exc), style="red", content_style="red")
            continue
        if command == "/context":
            print(render_context(operational_state.snapshot(), context_engine.status()))
            continue
        if command == "/status":
            snapshot = operational_state.snapshot()
            print(render_overview(snapshot))
            continue
        if command == "/spec":
            print(render_spec(operational_state.snapshot()))
            continue
        if command == "/tasks":
            print(render_tasks(operational_state.snapshot()))
            continue
        if command == "/agents":
            print(render_agents(operational_state.snapshot()))
            continue
        if command == "/trace" or command.startswith("/trace "):
            try:
                trace_limit = int(user_input.split(maxsplit=1)[1]) if " " in user_input else 20
            except ValueError:
                print_labeled("Trace>", "limite precisa ser inteiro.", style="red", content_style="red")
                continue
            print(render_trace(operational_state.snapshot(trace_limit=max(1, min(trace_limit, 160)))))
            continue
        if command == "/usage":
            print(render_usage(operational_state.snapshot(), metrics.snapshot().to_dict()))
            continue
        if command == "/checkpoint" or command.startswith("/checkpoint "):
            checkpoint_args = user_input[11:].strip()
            try:
                if not checkpoint_args:
                    checkpoints = checkpoint_manager.list_checkpoints()
                    if not checkpoints:
                        print("Checkpoints\n  No checkpoints in this workspace.")
                    else:
                        rows = [
                            (
                                item.checkpoint_id,
                                f"{'sealed' if item.sealed else 'open'} | rollback={'done' if item.rolled_back_at else 'available'}",
                            )
                            for item in checkpoints
                        ]
                        print(section("Checkpoints", rows))
                elif checkpoint_args.lower().startswith("rollback "):
                    checkpoint_id = checkpoint_args.split(maxsplit=1)[1].strip()
                    checkpoint = checkpoint_manager.load(checkpoint_id)
                    if not confirm_action(
                        "Rollback de checkpoint",
                        f"Restaurar {len(checkpoint.entries)} arquivo(s) de {checkpoint_id}. Edições posteriores causam recusa segura.",
                        destructive=True,
                    ):
                        print_labeled("Checkpoint>", "rollback não aprovado.", style="yellow", content_style="yellow")
                    else:
                        result = checkpoint_manager.rollback(checkpoint_id)
                        for relative_path in (*result.restored, *result.removed):
                            await codeintel_runtime.sync_written_file(config.workspace / relative_path)
                        print_labeled(
                            "Checkpoint>",
                            f"rollback concluído: restored={len(result.restored)}, removed={len(result.removed)}.",
                            style="green",
                            content_style="green",
                        )
                else:
                    checkpoint = checkpoint_manager.load(checkpoint_args)
                    print(section("Checkpoint", (
                        ("ID", checkpoint.checkpoint_id), ("Reason", redact_sensitive_text(checkpoint.reason)),
                        ("Created", checkpoint.created_at), ("Sealed", checkpoint.sealed),
                        ("Rolled back", bool(checkpoint.rolled_back_at)),
                    )))
            except (CheckpointError, ValueError) as exc:
                print_labeled("Checkpoint>", str(exc), style="red", content_style="red")
            continue
        if command == "/artifacts":
            print(render_artifacts(context_engine.list_artifacts()))
            continue
        if command == "/workspace":
            print(f"{CYAN}{config.workspace}{RESET}")
            continue
        if command == "/tools":
            print(section("Tools", ((tool["function"]["name"], "available; policy checked at execution") for tool in tool_schemas)))
            continue
        if command == "/tools schema":
            print(to_json(tool_schemas))
            continue
        if command == "/mcp":
            statuses = await mcp_registry.list_statuses()
            if not statuses:
                print(section("MCP", (("Provider", "not configured"), ("Connection", "inactive"), ("Capabilities", "none"), ("Active session", "none"), ("Policy", "governed; invocation remains opt-in"))))
            else:
                print(section("MCP", ((item.provider, f"{'connected' if item.available else 'unavailable'} | capabilities={','.join(item.capabilities) or 'none'} | policy=governed") for item in statuses)))
            continue
        if command == "/browser":
            print(section("Browser / Herd", (("Provider", "Herd via MCP (not configured)"), ("Connection", "inactive"), ("Capabilities", "none"), ("Active session", "none"), ("Content trust", "untrusted"), ("Policy", "governed"))))
            continue
        if command == "/herdr":
            herdr_status = await herdr_backend.status()
            print(section("Herdr terminal backend", (("Backend", herdr_status.name), ("Connection", "available" if herdr_status.available else "unavailable"), ("Persistent", herdr_status.persistent), ("Workspace", config.workspace), ("Pane", "none"), ("Task", "none"), ("State", "idle"), ("Fallback", "local mandatory"), ("DAG source of truth", "internal scheduler"), ("Reason", redact_sensitive_text(herdr_status.reason)))))
            continue
        if command == "/save" or command.startswith("/save ") or command == "/salvar" or command.startswith("/salvar "):
            parts = user_input.split(maxsplit=1)
            title_hint = parts[1].strip() if len(parts) > 1 else ""
            try:
                save_result = await save_conversation_history(
                    client=client,
                    model=model,
                    messages=messages,
                    config=config,
                    title_hint=title_hint,
                    prefer_model_summary=api_available,
                    save_reason="manual",
                )
            except (OSError, PermissionError, ValueError) as exc:
                print(f"{RED}Falha ao salvar histórico: {exc}{RESET}", file=sys.stderr)
                continue
            except KeyboardInterrupt:
                print()
                last_saved_digest = await persist_history_on_exit(
                    client, model, messages, config, last_saved_digest, "keyboard_interrupt", False
                )
                return

            last_saved_digest = save_result.transcript_digest
            print_labeled("Histórico>", f"salvo em {save_result.path}", style="green", content_style="green")
            if save_result.fallback_reason:
                print_labeled(
                    "Histórico>",
                    "resumo via modelo não ficou disponível; foi salva transcrição sanitizada.",
                    style="yellow",
                    content_style="yellow",
                )
            continue
        if command in {"/clear", "/limpar"}:
            messages = create_initial_messages(
                config,
                agent_profiles,
                skill_registry=skill_registry,
                spec_context=spec_context,
            )
            sync_context_state_message(messages, context_engine)
            last_saved_digest = None
            print(f"{YELLOW}Histórico limpo.{RESET}")
            continue

        turn_start = len(messages)
        tools_runner.subagents_started = 0
        if conversation_mode == "plan":
            try:
                exploration = await codeintel_runtime.prepare_for_workflow(
                    user_input,
                    for_goal=False,
                )
                plan = await run_plan_workflow(
                    client=client,
                    model=model,
                    messages=messages,
                    tools_runner=tools_runner,
                    store=PlanStore(config.workspace),
                    temperature=temperature,
                    config=config,
                    objective=user_input,
                    event_bus=event_bus,
                    exploration=exploration,
                )
            except OpenAIError as exc:
                api_available = False
                append_api_failure_context(messages, turn_start, exc)
                report_api_error(exc)
            except (ApprovalUnavailableError, PlanStoreError, PermissionError, ValueError) as exc:
                print_labeled("Plano>", str(exc), style="red", content_style="red")
            except PromptTooLargeError as exc:
                close_oversized_turn(messages, turn_start, exc)
                print_labeled("Contexto>", str(exc), style="red", content_style="red")
            else:
                api_available = True
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": format_plan_summary(plan)})
                print_assistant(format_plan_summary(plan))
            continue

        messages.append({"role": "user", "content": user_input})

        try:
            await run_agent_turn(
                client=client,
                model=model,
                messages=messages,
                tools_runner=tools_runner,
                tool_schemas=tool_schemas,
                temperature=temperature,
                max_steps=config.max_steps,
                api_retries=config.api_retries,
                event_bus=event_bus,
                reasoning_mode=main_reasoning_mode,
            )
        except OpenAIError as exc:
            api_available = False
            append_api_failure_context(messages, turn_start, exc)
            report_api_error(exc)
        except KeyboardInterrupt:
            print()
            last_saved_digest = await persist_history_on_exit(
                client, model, messages, config, last_saved_digest, "keyboard_interrupt", False
            )
            return
        except ApprovalUnavailableError as exc:
            del messages[turn_start:]
            print_labeled("Aprovação>", str(exc), style="red", content_style="red")
        except PromptTooLargeError as exc:
            close_oversized_turn(messages, turn_start, exc)
            print_labeled("Contexto>", str(exc), style="red", content_style="red")
        else:
            api_available = True


def build_config(args: argparse.Namespace) -> AgentConfig:
    workspace = Path(args.workspace).resolve()
    resources = packaged_resources(Path(__file__))
    api_key_file = resolve_user_file_path(args.api_key_file)
    model_alias_file = resolve_resource_path(
        args.model_alias_file,
        workspace=workspace,
        packaged_default=(
            args.model_alias_file == DEFAULT_MODEL_ALIAS_FILE
            and not os.getenv(MODEL_ALIAS_FILE_ENV)
        ),
        package_root=resources.root,
    )
    agents_file = resolve_resource_path(
        args.agents_file,
        workspace=workspace,
        packaged_default=args.agents_file == "AGENTS.md",
        package_root=resources.root,
    )
    skills_dir = resolve_resource_path(
        args.skills_dir,
        workspace=workspace,
        packaged_default=args.skills_dir == "skills",
        package_root=resources.root,
    )
    profiles_dir = resolve_resource_path(
        args.profiles_dir,
        workspace=workspace,
        packaged_default=(
            args.profiles_dir == DEFAULT_PROFILES_DIR
            and not os.getenv(PROFILES_DIR_ENV)
        ),
        package_root=resources.root,
    )
    if not workspace.exists():
        raise FileNotFoundError(f"Workspace não encontrado: {workspace}")
    if not workspace.is_dir():
        raise NotADirectoryError(f"Workspace não é diretório: {workspace}")
    if args.api_timeout < 5 or args.api_timeout > 300:
        raise ValueError("--api-timeout precisa estar entre 5 e 300 segundos.")
    if args.api_retries < 0 or args.api_retries > MAX_API_RETRIES:
        raise ValueError(f"--api-retries precisa estar entre 0 e {MAX_API_RETRIES}.")
    if args.history_limit < 1 or args.history_limit > MAX_HISTORY_FILES:
        raise ValueError(f"--history-limit precisa estar entre 1 e {MAX_HISTORY_FILES}.")
    if args.max_steps < 1 or args.max_steps > MAX_ALLOWED_STEPS:
        raise ValueError(f"--max-steps precisa estar entre 1 e {MAX_ALLOWED_STEPS}.")
    if args.max_subagents < 0 or args.max_subagents > 10:
        raise ValueError("--max-subagents precisa estar entre 0 e 10.")
    if args.subagent_max_steps < 1 or args.subagent_max_steps > MAX_ALLOWED_STEPS:
        raise ValueError(f"--subagent-max-steps precisa estar entre 1 e {MAX_ALLOWED_STEPS}.")
    if args.subagent_timeout < 60 or args.subagent_timeout > 86_400:
        raise ValueError("--subagent-timeout precisa estar entre 60 e 86400 segundos.")
    permission_mode = normalize_permission_mode(args.permission_mode)
    verbosity_mode = normalize_verbosity_mode(args.verbosity)

    return AgentConfig(
        workspace=workspace,
        api_key_file=api_key_file,
        model_alias_file=model_alias_file,
        agents_file=agents_file,
        skills_dir=skills_dir,
        profiles_dir=profiles_dir,
        read_scope=args.read_scope,
        write_scope=args.write_scope,
        load_project_context=not args.no_project_context,
        history_limit=args.history_limit,
        allow_shell=not args.no_shell,
        allow_sensitive_read=args.allow_sensitive_read,
        permission_mode=permission_mode,
        verbosity_mode=verbosity_mode,
        api_timeout_seconds=args.api_timeout,
        api_retries=args.api_retries,
        max_steps=args.max_steps,
        max_subagents=args.max_subagents,
        subagent_max_steps=args.subagent_max_steps,
        subagent_timeout_seconds=args.subagent_timeout,
        resource_root=resources.root,
    )


async def run_runtime_session(
    client: ModelAdapter,
    *,
    model: str,
    model_resolution: str,
    config: AgentConfig,
    temperature: float,
    agent_profiles: dict[str, AgentProfile],
    event_bus: EventBus | None = None,
) -> None:
    bus = event_bus or EventBus(logger=LOGGER)
    subscriptions: list[int] = []
    metrics = LocalMetricsCollector()
    operational_state = OperationalState()
    terminal_ui: TerminalUI | None = None
    lsp_config = os.getenv(LSP_CONFIG_ENV, "").strip()
    lsp_providers = load_lsp_providers(lsp_config) if lsp_config else ()
    codeintel_runtime = CodeIntelligenceRuntime(
        config.workspace,
        artifact_store=ArtifactStore(config.workspace),
        event_bus=bus,
        model_client=client,
        model=model,
        temperature=temperature,
        lsp_providers=lsp_providers,
    )
    metrics_subscription = bus.subscribe(metrics)
    state_subscription = bus.subscribe(operational_state)
    if event_bus is None:
        terminal_ui = TerminalUI(
            max_tasks=min(INITIAL_STEP_BUDGET, config.max_steps),
            max_visible_tasks=DEFAULT_MAX_VISIBLE_TASKS,
        )
        terminal_ui.subscribe(bus)
        subscriptions.append(bus.subscribe(StructuredEventLogger(LOGGER)))
    run_status = "completed"
    try:
        await bus.emit("run.started", source=AGENT_NAME, payload={"status": "running"})
        await agent_loop(
            client=client,
            model=model,
            model_resolution=model_resolution,
            config=config,
            temperature=temperature,
            agent_profiles=agent_profiles,
            event_bus=bus,
            operational_state=operational_state,
            metrics=metrics,
            codeintel_runtime=codeintel_runtime,
            terminal_ui=terminal_ui,
        )
    except BaseException:
        run_status = "failed"
        raise
    finally:
        await bus.emit("run.completed", source=AGENT_NAME, payload={"status": run_status})
        try:
            LocalMetricsStore(config.workspace).save(metrics.snapshot())
        except OSError as exc:
            LOGGER.warning("runtime_metrics_save_failed error=%s", type(exc).__name__)
        bus.unsubscribe(metrics_subscription)
        bus.unsubscribe(state_subscription)
        if terminal_ui is not None:
            terminal_ui.close()
        for subscription in subscriptions:
            bus.unsubscribe(subscription)
        await codeintel_runtime.close()
        await client.close()


def main() -> int:
    configure_stdio()
    args = parse_args()

    try:
        config = build_config(args)
        user_profile = load_or_select_profile(
            initial_profile=args.user_profile,
            interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        )
        config = replace(
            config,
            user_profile_id=user_profile.id,
            user_profile_what=user_profile.what,
            user_profile_criteria=user_profile.criteria,
            user_profile_skills=user_profile.skills,
        )
        configure_diagnostic_logging(config.workspace)
        agent_profiles = load_agent_profiles(config)
        model, model_resolution = resolve_model_name(
            direct_model=args.model,
            alias_name=args.model_alias,
            alias_file=config.model_alias_file,
        )
        api_key = read_api_key(config.api_key_file)
        client = build_client(
            api_key=api_key,
            base_url=args.base_url,
            timeout_seconds=config.api_timeout_seconds,
            model=model,
        )
    except (OSError, PermissionError, ValueError) as exc:
        print(f"{RED}Erro de configuração: {exc}{RESET}", file=sys.stderr)
        return 2

    try:
        asyncio.run(
            run_runtime_session(
                client,
                model=model,
                model_resolution=model_resolution,
                config=config,
                temperature=args.temperature,
                agent_profiles=agent_profiles,
            )
        )
    except KeyboardInterrupt:
        print_styled("Operação cancelada pelo usuário.", style="yellow")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
