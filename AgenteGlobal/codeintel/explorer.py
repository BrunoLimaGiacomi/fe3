"""Evidence-based, bounded and reusable codebase exploration."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from runtime.artifacts import ArtifactMetadata, ArtifactStore

from .index import CodeIndex
from .lsp import LSPManager, NavigationEvidence
from .models import Confidence, EvidenceRef, ParseStatus, RelationshipKind, StrictModel, SymbolKind


MAX_EXPLORATION_FILES = 24
MAX_EXPLORATION_SYMBOLS = 160
MAX_EXPLORATION_RELATIONSHIPS = 240
MAX_EXECUTION_FLOWS = 12
MAX_FLOW_STEPS = 10
DEFAULT_SOURCE_BUDGET_CHARS = 80_000
MAX_SOURCE_EXCERPT_CHARS = 4_000


class ExplorationDepth(StrEnum):
    TARGETED = "targeted"
    DEEP = "deep"


class Component(StrictModel):
    id: str
    name: str
    kind: str
    path: str
    symbols: tuple[str, ...] = ()
    responsibilities: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()


class ExplorationRelationship(StrictModel):
    source: str
    target: str
    kind: RelationshipKind
    confidence: Confidence
    evidence: tuple[EvidenceRef, ...] = ()
    origin: str


class ExecutionStep(StrictModel):
    step: int = Field(ge=1)
    component: str
    symbol: str
    path: str
    evidence: EvidenceRef
    relationship_to_next: str = ""


class ExecutionFlow(StrictModel):
    name: str
    steps: tuple[ExecutionStep, ...]
    confidence: Confidence


class ExplorationFinding(StrictModel):
    title: str
    description: str
    evidence: tuple[EvidenceRef, ...]
    confidence: Confidence


class ExplorationUnknown(StrictModel):
    question: str
    reason: str
    related_paths: tuple[str, ...] = ()


class ArchitectureGraph(StrictModel):
    nodes: tuple[str, ...] = ()
    edges: tuple[ExplorationRelationship, ...] = ()


class ExplorationMetrics(StrictModel):
    duration_seconds: float = Field(ge=0.0)
    files_considered: int = Field(ge=0)
    files_actually_read: int = Field(ge=0)
    source_chars_read: int = Field(ge=0)
    symbols_inspected: int = Field(ge=0)
    relationships_found: int = Field(ge=0)
    lsp_queries: int = Field(ge=0)
    cache_hit: bool = False
    estimated_context_tokens: int = Field(ge=0)
    context_utilization: float = Field(ge=0.0, le=1.0)


class ExplorationReport(StrictModel):
    schema_version: str = "1.0"
    report_id: str
    objective: str
    depth: ExplorationDepth
    generated_at: datetime
    index_updated_at: datetime
    scope_paths: tuple[str, ...]
    components: tuple[Component, ...]
    relationships: tuple[ExplorationRelationship, ...]
    execution_flows: tuple[ExecutionFlow, ...]
    findings: tuple[ExplorationFinding, ...]
    unknowns: tuple[ExplorationUnknown, ...]
    architecture_graph: ArchitectureGraph
    evidence: tuple[EvidenceRef, ...]
    supporting_hashes: dict[str, str]
    metrics: ExplorationMetrics
    potentially_stale: bool = False

    def compact_context(self, *, max_chars: int = 48_000) -> str:
        data = self.model_dump(mode="json", exclude={"supporting_hashes"})
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= max_chars:
            return encoded
        data["components"] = data["components"][:12]
        data["relationships"] = data["relationships"][:80]
        data["evidence"] = data["evidence"][:80]
        data["unknowns"] = data["unknowns"][:20]
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return encoded[:max_chars]


class ExplorationArtifacts(StrictModel):
    exploration_report: str
    architecture_graph: str
    execution_flow: str
    symbol_snapshot: str


class ExplorationRun(StrictModel):
    report: ExplorationReport
    artifacts: ExplorationArtifacts
    rendered: str
    synthesis: str = ""
    synthesis_error: str = Field(default="", max_length=1_000)
    reused: bool = False


_STOP_WORDS = frozenset(
    {
        "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em", "um", "uma",
        "como", "para", "por", "que", "the", "and", "or", "to", "of", "in", "how", "with",
        "entenda", "trace", "fluxo", "sistema", "codebase", "código", "codigo",
    }
)


def _keywords(objective: str) -> tuple[str, ...]:
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ_][A-Za-zÀ-ÖØ-öø-ÿ0-9_.-]{2,}", objective.casefold())
    return tuple(dict.fromkeys(word for word in words if word not in _STOP_WORDS))[:20]


def _safe_id(prefix: str, value: str) -> str:
    return prefix + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _evidence_key(item: EvidenceRef) -> tuple[str, int, int, str]:
    return (item.path.casefold(), item.start_line, item.end_line, item.symbol.casefold())


class CodebaseExplorer:
    def __init__(
        self,
        workspace: Path | str,
        index: CodeIndex,
        *,
        lsp_manager: LSPManager | None = None,
        max_files: int = MAX_EXPLORATION_FILES,
        max_symbols: int = MAX_EXPLORATION_SYMBOLS,
        max_relationships: int = MAX_EXPLORATION_RELATIONSHIPS,
        source_budget_chars: int = DEFAULT_SOURCE_BUDGET_CHARS,
        context_budget_tokens: int = 200_000,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        self.index = index
        self.lsp_manager = lsp_manager
        if min(max_files, max_symbols, max_relationships, source_budget_chars, context_budget_tokens) < 1:
            raise ValueError("exploration limits must be positive")
        self.max_files = max_files
        self.max_symbols = max_symbols
        self.max_relationships = max_relationships
        self.source_budget_chars = source_budget_chars
        self.context_budget_tokens = context_budget_tokens

    def _scope(self, objective: str, depth: ExplorationDepth) -> tuple[str, ...]:
        keywords = _keywords(objective)
        scores: dict[str, int] = {}
        for path, record in self.index.files.items():
            score = sum(4 for word in keywords if word in path.casefold())
            for symbol in record.symbols:
                score += sum(5 for word in keywords if word in symbol.name.casefold())
            if score:
                scores[path] = score
        if len(scores) < min(3, self.max_files):
            for word in keywords[:4]:
                for hit in self.index.lexical_search(word, limit=20):
                    scores[hit.path] = scores.get(hit.path, 0) + 1
        architecture_terms = {
            "arquitetura", "architecture", "componentes", "components",
            "dependências", "dependencias", "dependencies", "entradas", "entrypoints",
        }
        if depth is ExplorationDepth.DEEP and architecture_terms.intersection(keywords):
            entry_fragments = ("main", "app", "core", "cli", "api", "server", "handler", "runtime")
            candidates: list[tuple[int, str]] = []
            for path, record in self.index.files.items():
                lowered = path.casefold()
                if lowered.startswith(("tests/", "test/", "benchmarks/", ".")):
                    continue
                stem = Path(path).stem.casefold()
                score = min(6, len(record.imports)) + min(6, len(record.relationships) // 8)
                if any(fragment in stem for fragment in entry_fragments):
                    score += 8
                if Path(path).name == "__init__.py":
                    score += 3
                if score:
                    candidates.append((score, path))
            for score, path in sorted(candidates, key=lambda item: (-item[0], item[1]))[: self.max_files]:
                scores[path] = max(scores.get(path, 0), score)
        if not scores:
            entry_fragments = ("main", "app", "core", "cli", "api", "server", "handler", "__init__")
            for path in self.index.files:
                if any(fragment in Path(path).stem.casefold() for fragment in entry_fragments):
                    scores[path] = 1
        limit = min(self.max_files, 8 if depth is ExplorationDepth.TARGETED else self.max_files)
        return tuple(path for path, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit])

    def _components(self, paths: tuple[str, ...]) -> tuple[Component, ...]:
        components: list[Component] = []
        remaining = self.max_symbols
        per_file = max(1, self.max_symbols // max(1, len(paths)))
        for path in paths:
            record = self.index.files[path]
            symbols = tuple(record.symbols[: min(remaining, per_file)])
            remaining -= len(symbols)
            evidence = tuple(
                EvidenceRef(
                    path=path,
                    symbol=symbol.qualified_name,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    source=symbol.source,
                    confidence=Confidence.CONFIRMED,
                )
                for symbol in symbols[:12]
            )
            named = tuple(symbol.qualified_name for symbol in symbols if symbol.kind is not SymbolKind.MODULE)
            responsibilities: list[str] = []
            if record.imports:
                responsibilities.append(f"imports {len(record.imports)} módulos")
            if named:
                responsibilities.append(f"define {len(named)} símbolos")
            if record.parse_status is not ParseStatus.PARSED:
                responsibilities.append(f"análise {record.parse_status.value} via {record.parser_source}")
            components.append(
                Component(
                    id=_safe_id("component", path),
                    name=Path(path).stem,
                    kind="module" if record.language in {"python", "javascript", "typescript"} else "file",
                    path=path,
                    symbols=named[:40],
                    responsibilities=tuple(responsibilities),
                    evidence=evidence[:12] or (
                        EvidenceRef(path=path, start_line=1, end_line=1, source=record.parser_source),
                    ),
                )
            )
            if remaining <= 0:
                break
        return tuple(components)

    def _relationships(self, paths: tuple[str, ...], components: tuple[Component, ...]) -> tuple[ExplorationRelationship, ...]:
        selected_symbols = {symbol for component in components for symbol in component.symbols}
        selected_modules = {Path(path).with_suffix("").as_posix().replace("/", ".") for path in paths}
        local_roots = {
            part
            for path in paths
            for part in Path(path).with_suffix("").parts[:-1]
            if part not in {"src", "."}
        }
        candidates: list[ExplorationRelationship] = []
        for path in paths:
            for item in self.index.files[path].relationships:
                if (
                    item.source_symbol in selected_symbols
                    or item.target_symbol in selected_symbols
                    or item.source_symbol in selected_modules
                    or item.kind in {RelationshipKind.IMPORTS, RelationshipKind.CALLS, RelationshipKind.INHERITS}
                ):
                    if not item.evidence:
                        continue
                    candidates.append(
                        ExplorationRelationship(
                            source=item.source_symbol,
                            target=item.target_symbol,
                            kind=item.kind,
                            confidence=item.confidence,
                            evidence=item.evidence,
                            origin=item.origin,
                        )
                    )
                    if len(candidates) >= self.max_relationships * 8:
                        break

        def priority(item: ExplorationRelationship) -> tuple[int, str, str]:
            target = item.target_symbol if hasattr(item, "target_symbol") else item.target
            target_root = target.lstrip(".").split(".", 1)[0]
            local_symbol = target in selected_symbols or any(
                symbol.endswith(f".{target}") for symbol in selected_symbols if target
            )
            if item.kind is RelationshipKind.IMPORTS and target_root in local_roots:
                rank = 0
            elif item.kind in {RelationshipKind.INHERITS, RelationshipKind.IMPLEMENTS}:
                rank = 1
            elif item.kind in {RelationshipKind.CALLS, RelationshipKind.INVOKES} and local_symbol:
                rank = 2
            elif item.kind is RelationshipKind.IMPORTS:
                rank = 3
            elif item.kind in {RelationshipKind.CALLS, RelationshipKind.INVOKES}:
                rank = 4
            else:
                rank = 5
            evidence = item.evidence[0]
            return (rank, evidence.path, f"{evidence.start_line:09d}")

        candidates.sort(key=priority)
        unique: list[ExplorationRelationship] = []
        seen: set[tuple[str, str, RelationshipKind]] = set()
        for item in candidates:
            key = (item.source, item.target, item.kind)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= self.max_relationships:
                break
        return tuple(unique)

    def _flows(self, relationships: tuple[ExplorationRelationship, ...]) -> tuple[ExecutionFlow, ...]:
        calls = [item for item in relationships if item.kind in {RelationshipKind.CALLS, RelationshipKind.INVOKES}]
        adjacency: dict[str, list[ExplorationRelationship]] = {}
        targets = {item.target for item in calls}
        for item in calls:
            adjacency.setdefault(item.source, []).append(item)
        roots = [source for source in adjacency if source not in targets] or list(adjacency)
        flows: list[ExecutionFlow] = []
        for root in roots[:MAX_EXECUTION_FLOWS]:
            steps: list[ExecutionStep] = []
            current = root
            visited: set[str] = set()
            confidence = Confidence.CONFIRMED
            while current in adjacency and current not in visited and len(steps) < MAX_FLOW_STEPS:
                visited.add(current)
                edge = sorted(adjacency[current], key=lambda item: (item.target, item.origin))[0]
                ev = edge.evidence[0]
                if edge.confidence is not Confidence.CONFIRMED:
                    confidence = Confidence.INFERRED
                steps.append(
                    ExecutionStep(
                        step=len(steps) + 1,
                        component=Path(ev.path).stem,
                        symbol=current,
                        path=ev.path,
                        evidence=ev,
                        relationship_to_next=edge.kind.value,
                    )
                )
                current = edge.target
            if steps:
                last = steps[-1]
                steps.append(
                    ExecutionStep(
                        step=len(steps) + 1,
                        component=last.component,
                        symbol=current,
                        path=last.path,
                        evidence=last.evidence,
                    )
                )
                flows.append(ExecutionFlow(name=f"{root} flow", steps=tuple(steps), confidence=confidence))
        return tuple(flows)

    async def _lsp_evidence(
        self, components: tuple[Component, ...]
    ) -> tuple[tuple[EvidenceRef, ...], int, tuple[str, ...]]:
        if self.lsp_manager is None:
            return (), 0, ()
        configured = frozenset(self.lsp_manager.configured_languages())
        if not configured:
            return (), 0, ()
        gathered: list[EvidenceRef] = []
        failures: list[str] = []
        queries = 0
        for component in components[:4]:
            record = self.index.files.get(component.path)
            if record is None or record.language not in configured or not component.symbols:
                continue
            symbol = component.symbols[0].rsplit(".", 1)[-1]
            queries += 1
            try:
                response = await self.lsp_manager.navigate(
                    "definition", language=record.language, query=symbol, path=component.path
                )
            except Exception as error:  # optional provider boundary; fallback remains local
                failures.append(f"{record.language} ({component.path}): {error}")
                continue
            gathered.extend(item.evidence for item in response.evidence[:8])
            if response.error:
                failures.append(f"{record.language} ({component.path}): {response.error}")
        return tuple(gathered), queries, tuple(failures)

    def _verify_sources(self, paths: tuple[str, ...]) -> tuple[int, int]:
        files_read = chars_read = 0
        for relative in paths:
            if chars_read >= self.source_budget_chars:
                break
            candidate = (self.workspace / Path(relative)).resolve(strict=False)
            try:
                candidate.relative_to(self.workspace)
                remaining = min(MAX_SOURCE_EXCERPT_CHARS, self.source_budget_chars - chars_read)
                with candidate.open("r", encoding="utf-8", errors="replace") as stream:
                    excerpt = stream.read(remaining)
            except (OSError, ValueError):
                continue
            files_read += 1
            chars_read += len(excerpt)
        return files_read, chars_read

    async def explore(self, objective: str, *, depth: ExplorationDepth = ExplorationDepth.DEEP) -> ExplorationReport:
        objective = objective.strip()
        if not objective:
            raise ValueError("exploration objective cannot be empty")
        started = time.perf_counter()
        paths = self._scope(objective, depth)
        components = self._components(paths)
        relationships = self._relationships(paths, components)
        flows = self._flows(relationships)
        lsp_evidence, lsp_queries, lsp_failures = await self._lsp_evidence(components)
        files_read, chars_read = self._verify_sources(paths)

        evidence_map: dict[tuple[str, int, int, str], EvidenceRef] = {}
        for component in components:
            for item in component.evidence:
                evidence_map[_evidence_key(item)] = item
        for relation in relationships:
            for item in relation.evidence:
                evidence_map[_evidence_key(item)] = item
        for item in lsp_evidence:
            evidence_map[_evidence_key(item)] = item

        findings: list[ExplorationFinding] = []
        unknowns: list[ExplorationUnknown] = []
        for path in paths:
            record = self.index.files[path]
            if record.parse_status in {ParseStatus.MALFORMED, ParseStatus.SKIPPED_LARGE}:
                ev = EvidenceRef(path=path, source=record.parser_source, confidence=Confidence.CONFIRMED)
                findings.append(
                    ExplorationFinding(
                        title=f"Análise incompleta de {path}",
                        description=record.parse_error or record.parse_status.value,
                        evidence=(ev,),
                        confidence=Confidence.CONFIRMED,
                    )
                )
            elif record.parse_status in {ParseStatus.FALLBACK, ParseStatus.UNSUPPORTED}:
                unknowns.append(
                    ExplorationUnknown(
                        question=f"Quais relações semânticas existem em {path}?",
                        reason=f"Backend disponível: {record.parser_source}; status={record.parse_status.value}.",
                        related_paths=(path,),
                    )
                )
        if not paths:
            unknowns.append(
                ExplorationUnknown(
                    question="Qual parte do repositório implementa o objetivo?",
                    reason="Nenhum arquivo ou símbolo relevante foi localizado pelo índice.",
                )
            )
        if not flows:
            unknowns.append(
                ExplorationUnknown(
                    question="Qual é o fluxo de execução completo?",
                    reason="Não há relações calls/invokes suficientes para reconstrução estática.",
                    related_paths=paths[:8],
                )
            )
        if self.lsp_manager is None or not self.lsp_manager.configured_languages():
            unknowns.append(
                ExplorationUnknown(
                    question="Há definições dinâmicas que somente um language server resolveria?",
                    reason="Nenhum LSP está configurado; AST/índice/lexical permanecem ativos.",
                    related_paths=paths[:8],
                )
            )
        elif lsp_failures:
            # Static/lexical fallback is still authoritative for what it
            # found, but a configured provider failure must remain visible so
            # the report cannot overstate semantic coverage.
            unknowns.append(
                ExplorationUnknown(
                    question="Quais definições dinâmicas o LSP indisponível não conseguiu resolver?",
                    reason=(
                        "LSP configurado, mas indisponível ou com falha durante a exploração; "
                        "AST/índice/lexical foram usados como fallback. "
                        + "; ".join(lsp_failures)
                    )[:4_000],
                    related_paths=paths[:8],
                )
            )

        estimated_tokens = max(1, (chars_read + len(relationships) * 160 + len(components) * 240) // 3)
        utilization = min(1.0, estimated_tokens / self.context_budget_tokens)
        metrics = ExplorationMetrics(
            duration_seconds=max(0.0, time.perf_counter() - started),
            files_considered=len(paths),
            files_actually_read=files_read,
            source_chars_read=chars_read,
            symbols_inspected=sum(len(item.symbols) for item in components),
            relationships_found=len(relationships),
            lsp_queries=lsp_queries,
            estimated_context_tokens=estimated_tokens,
            context_utilization=utilization,
        )
        graph = ArchitectureGraph(
            nodes=tuple(dict.fromkeys([item.source for item in relationships] + [item.target for item in relationships])),
            edges=relationships,
        )
        generated = datetime.now(timezone.utc)
        report_id = _safe_id(
            "exploration",
            f"{objective}\0{depth.value}\0{self.index.metadata.updated_at.isoformat()}",
        )
        return ExplorationReport(
            report_id=report_id,
            objective=objective,
            depth=depth,
            generated_at=generated,
            index_updated_at=self.index.metadata.updated_at,
            scope_paths=paths,
            components=components,
            relationships=relationships,
            execution_flows=flows,
            findings=tuple(findings),
            unknowns=tuple(unknowns),
            architecture_graph=graph,
            evidence=tuple(evidence_map.values()),
            supporting_hashes={path: self.index.files[path].content_hash for path in paths},
            metrics=metrics,
        )


def report_is_stale(report: ExplorationReport, index: CodeIndex) -> bool:
    return any(index.files.get(path) is None or index.files[path].content_hash != digest for path, digest in report.supporting_hashes.items())


def render_mermaid(graph: ArchitectureGraph) -> str:
    if not graph.edges:
        return ""
    node_ids = {node: f"N{number}" for number, node in enumerate(graph.nodes, 1)}

    def label(value: str) -> str:
        return value.replace('"', "'").replace("\n", " ")[:80]

    lines = ["flowchart LR"]
    for node, identifier in node_ids.items():
        lines.append(f'  {identifier}["{label(node)}"]')
    for edge in graph.edges:
        source = node_ids.setdefault(edge.source, f"N{len(node_ids) + 1}")
        target = node_ids.setdefault(edge.target, f"N{len(node_ids) + 1}")
        lines.append(f"  {source} -->|{edge.kind.value}| {target}")
    return "\n".join(lines)


def render_report(report: ExplorationReport) -> str:
    lines = [
        f"Exploração: {report.objective}",
        f"Profundidade: {report.depth.value} | Componentes: {len(report.components)} | Relações: {len(report.relationships)}",
        "",
        "Componentes:",
    ]
    for component in report.components:
        evidence = component.evidence[0]
        lines.append(f"- {component.name} ({component.path}:{evidence.start_line}-{evidence.end_line})")
    if report.execution_flows:
        lines.extend(["", "Fluxos de execução:"])
        for flow in report.execution_flows:
            chain = " -> ".join(step.symbol for step in flow.steps)
            evidence = flow.steps[0].evidence
            lines.append(f"- {chain} [{flow.confidence.value}; {evidence.path}:{evidence.start_line}]")
    if report.findings:
        lines.extend(["", "Achados:"])
        for finding in report.findings:
            ev = finding.evidence[0]
            lines.append(f"- {finding.title}: {finding.description} ({ev.path}:{ev.start_line})")
    if report.unknowns:
        lines.extend(["", "Desconhecidos:"])
        lines.extend(f"- {item.question} — {item.reason}" for item in report.unknowns)
    diagram = render_mermaid(report.architecture_graph)
    if diagram:
        lines.extend(["", "```mermaid", diagram, "```"])
    return "\n".join(lines)


class ExplorationStore:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    @staticmethod
    def objective_hash(objective: str) -> str:
        return hashlib.sha256(objective.strip().casefold().encode("utf-8")).hexdigest()

    def persist(self, report: ExplorationReport) -> ExplorationArtifacts:
        common = {
            "report_id": report.report_id,
            "objective_hash": self.objective_hash(report.objective),
            "depth": report.depth.value,
            "index_updated_at": report.index_updated_at.isoformat(),
        }
        graph = self.artifact_store.put(
            report.architecture_graph.model_dump_json(indent=2),
            summary="Grafo arquitetural derivado de exploração evidence-based.",
            media_type="application/json; charset=utf-8",
            metadata={**common, "kind": "architecture_graph"},
        )
        flow = self.artifact_store.put(
            json.dumps([item.model_dump(mode="json") for item in report.execution_flows], ensure_ascii=False, indent=2),
            summary="Fluxos de execução derivados da exploração.",
            media_type="application/json; charset=utf-8",
            metadata={**common, "kind": "execution_flow"},
        )
        snapshot = self.artifact_store.put(
            json.dumps(
                {
                    "report_id": report.report_id,
                    "scope_paths": report.scope_paths,
                    "supporting_hashes": report.supporting_hashes,
                    "evidence": [item.model_dump(mode="json") for item in report.evidence],
                },
                ensure_ascii=False,
                indent=2,
            ),
            summary="Snapshot compacto de símbolos e evidências da exploração.",
            media_type="application/json; charset=utf-8",
            metadata={**common, "kind": "symbol_snapshot"},
        )
        report_artifact = self.artifact_store.put(
            report.model_dump_json(indent=2),
            summary=f"ExplorationReport: {report.objective[:200]}",
            media_type="application/json; charset=utf-8",
            metadata={
                **common,
                "kind": "exploration_report",
                "architecture_graph": graph.artifact_id,
                "execution_flow": flow.artifact_id,
                "symbol_snapshot": snapshot.artifact_id,
            },
        )
        return ExplorationArtifacts(
            exploration_report=report_artifact.artifact_id,
            architecture_graph=graph.artifact_id,
            execution_flow=flow.artifact_id,
            symbol_snapshot=snapshot.artifact_id,
        )

    def find_reusable(
        self,
        objective: str,
        depth: ExplorationDepth,
        index: CodeIndex,
    ) -> tuple[ExplorationReport, ArtifactMetadata] | None:
        wanted = self.objective_hash(objective)
        for metadata in self.artifact_store.list(limit=200):
            values = metadata.metadata
            if (
                values.get("kind") != "exploration_report"
                or values.get("objective_hash") != wanted
                or values.get("depth") != depth.value
            ):
                continue
            try:
                report = ExplorationReport.model_validate_json(
                    self.artifact_store.read_text(metadata.artifact_id)
                )
            except (ValueError, OSError):
                continue
            if not report_is_stale(report, index):
                return report, metadata
        return None


__all__ = [
    "ArchitectureGraph",
    "CodebaseExplorer",
    "Component",
    "ExecutionFlow",
    "ExecutionStep",
    "ExplorationArtifacts",
    "ExplorationDepth",
    "ExplorationFinding",
    "ExplorationMetrics",
    "ExplorationRelationship",
    "ExplorationReport",
    "ExplorationRun",
    "ExplorationStore",
    "ExplorationUnknown",
    "render_mermaid",
    "render_report",
    "report_is_stale",
]
