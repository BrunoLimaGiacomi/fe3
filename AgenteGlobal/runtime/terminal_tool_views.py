"""Safe, compact descriptions of tool activity for terminal presentation."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from typing import Any

from .security_text import redact_cli_args, redact_command_text, redact_sensitive_text, truncate_single_line


def _path(path: str | None, path_reference: str | None = None) -> str:
    selected = str(path or ".")
    reference = str(path_reference or "").strip()
    return f"{reference}:{selected}" if reference else selected


def describe_tool_activity(
    tool_name: str,
    arguments: dict[str, Any],
    step: int,
    max_steps: int,
    *,
    default_timeout: int = 60,
    default_max_search_files: int = 20_000,
) -> str:
    prefix = f"{step}/{max_steps} - {tool_name}: "
    if tool_name == "run_cli":
        cli = str(arguments.get("cli", "")).strip()
        args = arguments.get("args") or []
        safe_args = redact_cli_args(args) if isinstance(args, list) else []
        command = subprocess.list2cmdline([cli, *safe_args]) if cli else "(CLI not provided)"
        return prefix + f"running CLI `{truncate_single_line(command)}` (timeout {arguments.get('timeout_seconds', default_timeout)}s)."
    if tool_name == "run_powershell":
        command = redact_command_text(str(arguments.get("command", "")))
        return prefix + f"running PowerShell `{truncate_single_line(command)}` (timeout {arguments.get('timeout_seconds', default_timeout)}s)."
    if tool_name == "write_file":
        content = str(arguments.get("content", ""))
        path = _path(arguments.get("path"), arguments.get("path_reference"))
        return prefix + f"writing `{path}` ({len(content.encode('utf-8'))} bytes, overwrite={bool(arguments.get('overwrite', False))})."
    if tool_name == "read_file":
        path = _path(arguments.get("path"), arguments.get("path_reference"))
        return prefix + f"reading `{path}` from line {arguments.get('start_line', 1)}, up to {arguments.get('max_lines', 200)} lines."
    if tool_name == "list_dir":
        path = _path(arguments.get("path", "."), arguments.get("path_reference"))
        return prefix + f"listing up to {arguments.get('max_entries', 100)} entries in `{path}`."
    if tool_name == "search_text":
        pattern = truncate_single_line(str(arguments.get("pattern", "")), limit=120)
        path = _path(arguments.get("path", "."), arguments.get("path_reference"))
        return prefix + f"searching `{pattern}` in `{path}` (up to {arguments.get('max_matches', 50)} matches, {arguments.get('max_scanned_files', default_max_search_files)} files)."
    if tool_name == "spawn_subagent":
        name = str(arguments.get("name", "subagent"))
        profile = str(arguments.get("profile", "") or "automatic")
        task = truncate_single_line(str(arguments.get("task", "")), limit=180)
        return prefix + f"starting `{name}` (profile={profile}, mutation={bool(arguments.get('allow_mutation', False))}) for `{task}`."
    return prefix + "running model-requested tool."


def summarize_tool_result(tool_name: str, result: str) -> tuple[str, str]:
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return "green", f"{tool_name}: completed; textual result with {len(result)} characters."
    if isinstance(parsed, dict) and parsed.get("error"):
        message = truncate_single_line(redact_sensitive_text(str(parsed.get("message", ""))), 180)
        error = truncate_single_line(redact_sensitive_text(str(parsed.get("error"))), 100)
        return "red", f"{tool_name}: falhou ({error}){f' - {message}' if message else ''}"
    if tool_name in {"run_cli", "run_powershell"} and isinstance(parsed, dict):
        code = parsed.get("returncode")
        stdout_len = len(str(parsed.get("stdout") or ""))
        stderr_len = len(str(parsed.get("stderr") or ""))
        style = "green" if code == 0 else "red"
        if code == 0:
            # A successful process is represented by the task state.  The
            # noisy ``exit 0`` detail is intentionally omitted from the CLI.
            return "green", f"{tool_name}: concluído (stdout={stdout_len}, stderr={stderr_len})."
        return style, f"{tool_name}: failed (exit {code}; stdout={stdout_len}, stderr={stderr_len})."
    if tool_name == "write_file" and isinstance(parsed, dict):
        return "green", f"write_file: wrote `{parsed.get('absolute_path') or parsed.get('path')}`."
    if tool_name == "read_file" and isinstance(parsed, dict):
        return "green", f"read_file: returned {parsed.get('returned_lines')} lines from `{parsed.get('path')}`."
    if tool_name == "list_dir" and isinstance(parsed, dict):
        entries = parsed.get("entries") if isinstance(parsed.get("entries"), list) else []
        return "green", f"list_dir: {len(entries)} of {parsed.get('total_entries', len(entries))} entries."
    if tool_name == "search_text" and isinstance(parsed, dict):
        matches = parsed.get("matches") if isinstance(parsed.get("matches"), list) else []
        return "green", f"search_text: {len(matches)} matches; {parsed.get('scanned_files')} files read."
    if tool_name == "spawn_subagent" and isinstance(parsed, dict):
        status = parsed.get("status", "completed")
        style = "red" if status in {"error", "failed"} else "green" if status in {"completed", "concluído"} else "yellow"
        return style, f"spawn_subagent: status={status}, subagent={parsed.get('subagent') or parsed.get('name')}."
    return "green", f"{tool_name}: completed."


def infer_assistant_content_style(content: str) -> str:
    lowered = content.lower()
    first_line = lowered.lstrip().splitlines()[0] if lowered.strip() else ""
    error_starts = ("erro", "falha", "falhou", "negado", "traceback", "unauthorized", "forbidden")
    success_starts = (
        "sucesso", "concluído", "concluída", "finalizado", "finalizada",
        "validado", "validada", "resolvido", "resolvida", "salvo", "salva",
    )
    # Do not paint a complete report red merely because it discusses an error
    # or a security finding in a later paragraph.
    if first_line.startswith(error_starts):
        return "red"
    return "green" if first_line.startswith(success_starts) else "white"


def _field(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _safe_label(value: Any, limit: int = 100) -> str:
    return truncate_single_line(redact_sensitive_text(str(value or "")), limit=limit)


def _evidence_label(item: Any) -> str:
    evidence = _field(item, "evidence")
    if isinstance(evidence, (list, tuple)):
        evidence = evidence[0] if evidence else None
    if evidence is None:
        return ""
    path = _safe_label(_field(evidence, "path"), 120)
    start = _field(evidence, "start_line")
    end = _field(evidence, "end_line")
    if not path:
        return ""
    if isinstance(start, int) and isinstance(end, int) and end != start:
        return f"{path}:{start}-{end}"
    return f"{path}:{start}" if isinstance(start, int) else path


def _compact_symbol(value: Any, limit: int = 72) -> str:
    """Keep terminal graph labels readable without losing their identity."""

    label = _safe_label(value, limit=160)
    parts = label.split(".")
    if len(parts) > 3:
        label = ".".join(parts[-3:])
    return truncate_single_line(label, limit=limit)


def _relationship_kind(item: Any) -> str:
    value = _field(item, "kind")
    return str(getattr(value, "value", value) or "").lower()


def _internal_roots(edges: list[Any]) -> set[str]:
    """Infer local package roots from evidence paths, never from model prose."""

    roots: set[str] = set()
    for edge in edges:
        evidence = _field(edge, "evidence")
        if isinstance(evidence, (list, tuple)):
            evidence = evidence[0] if evidence else None
        path = str(_field(evidence, "path", "") or "").replace("\\", "/")
        if not path:
            continue
        parts = [part for part in path.split("/") if part and part not in {".", "src"}]
        if len(parts) > 1:
            roots.add(parts[0].removesuffix(".py"))
        elif parts:
            roots.add(parts[0].removesuffix(".py"))
    return roots


def _select_architecture_edges(
    edges: list[Any],
    limit: int,
    *,
    known_local_roots: set[str] | None = None,
) -> list[Any]:
    """Prefer architectural relations and local imports over dependency noise."""

    roots = _internal_roots(edges) | set(known_local_roots or ())
    source_symbols = {str(_field(edge, "source", "") or "") for edge in edges}
    architectural = {
        "inherits", "implements", "depends_on",
        "reads", "writes", "creates", "publishes", "consumes",
    }
    internal_imports: list[Any] = []
    internal_calls: list[Any] = []
    other_relations: list[Any] = []
    for edge in edges:
        kind = _relationship_kind(edge)
        target = str(_field(edge, "target", "") or "").lstrip(".")
        target_root = target.split(".", 1)[0]
        target_is_local_symbol = target in source_symbols or any(
            source.endswith(f".{target}") for source in source_symbols if target
        )
        if kind == "imports" and target_root in roots:
            internal_imports.append(edge)
        elif kind in {"calls", "invokes"} and target_is_local_symbol:
            internal_calls.append(edge)
        elif kind in architectural:
            other_relations.append(edge)
    selected = (internal_imports + internal_calls + other_relations)[:limit]
    if selected:
        return selected
    # Small/non-package codebases still get a useful bounded graph.
    return [edge for edge in edges if _relationship_kind(edge) not in {"defines", "references"}][:limit]


def render_architecture_graph(
    graph: Any,
    *,
    use_unicode: bool = True,
    max_nodes: int = 24,
    max_edges: int = 30,
    local_roots: set[str] | None = None,
) -> str:
    """Render an architecture graph as terminal-safe ASCII or Unicode.

    ``graph`` is deliberately duck-typed so the view can consume the typed
    ``ArchitectureGraph`` without importing the code-intelligence package.
    Only node/edge metadata and EvidenceRefs are rendered; model text and
    reasoning fields are never interpolated.
    """

    edges_raw = _field(graph, "edges", ()) if graph is not None else ()
    candidate_edges = list(edges_raw or ())[: max(1, max_edges) * 6]
    edges = _select_architecture_edges(
        candidate_edges,
        max(1, max_edges),
        known_local_roots=local_roots,
    )
    nodes: list[str] = []
    # Keep graph output bounded even when a malformed object advertises a huge
    # iterable.  Edges are allowed to introduce a node absent from ``nodes``.
    for edge in edges:
        for endpoint in (_field(edge, "source"), _field(edge, "target")):
            label = _compact_symbol(endpoint)
            if label and label not in nodes and len(nodes) < max(1, max_nodes):
                nodes.append(label)
    if not nodes and not edges:
        return "Arquitetura\n  Nenhuma relação observada."

    outgoing: dict[str, list[Any]] = {node: [] for node in nodes}
    for edge in edges:
        source = _compact_symbol(_field(edge, "source"))
        if source in outgoing:
            outgoing[source].append(edge)
    arrow = "→" if use_unicode else "->"
    branch = "├─" if use_unicode else "|-"
    tail = "└─" if use_unicode else "\\-"
    lines = ["Arquitetura (evidence-based)"]
    for node in nodes:
        related = outgoing.get(node, ())
        if not related:
            continue
        lines.append(f"  [{node}]")
        import_groups: dict[str, list[Any]] = {}
        for edge in related:
            if _relationship_kind(edge) == "imports":
                root = str(_field(edge, "target", "") or "?").lstrip(".").split(".", 1)[0]
                import_groups.setdefault(root, []).append(edge)
        rows: list[tuple[str, str, str, str]] = []
        emitted_groups: set[str] = set()
        for edge in related:
            target = _compact_symbol(_field(edge, "target")) or "?"
            kind = _relationship_kind(edge)
            if kind == "imports":
                root = str(_field(edge, "target", "") or "?").lstrip(".").split(".", 1)[0]
                grouped = import_groups.get(root, [])
                if len(grouped) >= 3:
                    if root in emitted_groups:
                        continue
                    emitted_groups.add(root)
                    target = f"{root} ({len(grouped)} modules)"
            confidence = _safe_label(getattr(_field(edge, "confidence"), "value", _field(edge, "confidence")), 30)
            rows.append((target, kind, confidence, _evidence_label(edge)))
        for index, (target, kind, confidence, evidence) in enumerate(rows):
            prefix = tail if index == len(rows) - 1 else branch
            detail = ", ".join(value for value in (kind, confidence, evidence) if value)
            suffix = f" [{detail}]" if detail else ""
            lines.append(f"  {prefix} {arrow} {target}{suffix}")
    return "\n".join(lines)


def render_execution_flows(
    flows: Any,
    *,
    use_unicode: bool = True,
    max_flows: int = 8,
    max_steps: int = 16,
) -> str:
    """Render bounded execution flows without Mermaid or hidden reasoning."""

    values = list(flows or ())
    operational_terms = (
        "main", "run", "execute", "agent", "handle", "dispatch", "explore",
        "workflow", "start", "process", "request",
    )
    values.sort(
        key=lambda flow: (
            0 if any(term in str(_field(flow, "name", "")).casefold() for term in operational_terms) else 1,
            str(_field(flow, "name", "")).casefold(),
        )
    )
    values = values[: max(1, max_flows)]
    if not values:
        return "Fluxos de execução\n  Nenhum fluxo observado."
    connector = "  ↓" if use_unicode else "  |\n  v"
    lines = ["Fluxos de execução"]
    for flow in values:
        name = _safe_label(_field(flow, "name") or "fluxo")
        confidence = _safe_label(getattr(_field(flow, "confidence"), "value", _field(flow, "confidence")), 30)
        lines.append(f"  {name}{f' [{confidence}]' if confidence else ''}")
        steps = list(_field(flow, "steps", ()) or ())[: max(1, max_steps)]
        for number, step in enumerate(steps, 1):
            symbol = _safe_label(_field(step, "symbol") or _field(step, "component") or "step")
            evidence = _evidence_label(step)
            suffix = f" ({evidence})" if evidence else ""
            lines.append(f"    {number}. {symbol}{suffix}")
            if number < len(steps):
                lines.append(f"    {connector}")
    return "\n".join(lines)


def render_exploration_report(
    report: Any,
    *,
    use_unicode: bool = True,
    max_components: int = 32,
    max_findings: int = 24,
    max_unknowns: int = 20,
) -> str:
    """Render the safe terminal projection used by ``/explore``."""

    if report is None:
        return "Exploração\n  Nenhum relatório disponível."
    objective = _safe_label(_field(report, "objective") or "exploração", 180)
    depth = _safe_label(getattr(_field(report, "depth"), "value", _field(report, "depth")), 30)
    components = list(_field(report, "components", ()) or ())[: max(1, max_components)]
    lines = [f"Exploração: {objective}"]
    if depth:
        lines.append(f"Profundidade: {depth}")
    lines.append("")
    lines.append("Componentes")
    if components:
        for component in components:
            name = _safe_label(_field(component, "name") or _field(component, "id") or "componente")
            path = _safe_label(_field(component, "path"), 140)
            evidence = _evidence_label(component)
            location = evidence or path
            lines.append(f"  • {name}{f' ({location})' if location else ''}")
    else:
        lines.append("  Nenhum componente observado.")

    graph = _field(report, "architecture_graph")
    if graph is not None:
        local_roots = {
            part
            for component in components
            for part in str(_field(component, "path", "") or "").replace("\\", "/").split("/")[:-1]
            if part and part not in {".", "src"}
        }
        lines.extend([
            "",
            render_architecture_graph(graph, use_unicode=use_unicode, local_roots=local_roots),
        ])
    flows = _field(report, "execution_flows", ())
    lines.extend(["", render_execution_flows(flows, use_unicode=use_unicode)])

    findings = list(_field(report, "findings", ()) or ())[: max(1, max_findings)]
    if findings:
        lines.extend(["", "Achados"])
        for finding in findings:
            title = _safe_label(_field(finding, "title") or "achado", 140)
            evidence = _evidence_label(finding)
            lines.append(f"  • {title}{f' ({evidence})' if evidence else ''}")

    unknowns = list(_field(report, "unknowns", ()) or ())[: max(1, max_unknowns)]
    if unknowns:
        lines.extend(["", "Desconhecidos"])
        for unknown in unknowns:
            question = _safe_label(_field(unknown, "question") or "questão", 180)
            lines.append(f"  ? {question}")
    return "\n".join(lines)


# Friendly aliases for callers that do not need to distinguish graph/report
# terminology.  Keep the old Mermaid renderer in code-intelligence for its
# artifact contract; terminal paths should import these functions instead.
render_architecture = render_architecture_graph
render_execution_flow = render_execution_flows
render_exploration = render_exploration_report


__all__ = [
    "describe_tool_activity",
    "infer_assistant_content_style",
    "render_architecture",
    "render_architecture_graph",
    "render_execution_flow",
    "render_execution_flows",
    "render_exploration",
    "render_exploration_report",
    "summarize_tool_result",
]
