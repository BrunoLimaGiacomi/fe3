"""Composition boundary for index, LSP, exploration, artifacts and model synthesis."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from llm.base import ModelAdapter, ModelRequest
from llm.reasoning import ReasoningMode
from runtime.artifacts import ArtifactStore
from runtime.context_budget import BudgetCategory, ContextBudget

from .explorer import (
    CodebaseExplorer,
    ExplorationArtifacts,
    ExplorationDepth,
    ExplorationRun,
    ExplorationStore,
    render_report,
)
from .admission import (
    ExplorationAdmissionController,
    ExplorationLevel,
    ExplorationPreparation,
    build_exploration_directive,
)
from .index import CodeIndex, CodeIndexer
from .documents import DiagnosticRecord
from .lsp import EventEmitter, LSPManager, LanguageServerProvider


class CodeIntelligenceRuntime:
    """Lazy, read-only orchestration facade used by CLI/workflow integrations."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        artifact_store: ArtifactStore,
        event_bus: EventEmitter | None = None,
        lsp_providers: tuple[LanguageServerProvider, ...] = (),
        model_client: ModelAdapter | None = None,
        model: str = "",
        temperature: float = 0.0,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        self.artifact_store = artifact_store
        self.event_bus = event_bus
        self.lsp_providers = lsp_providers
        self.model_client = model_client
        self.model = model
        self.temperature = temperature
        self.indexer = CodeIndexer(self.workspace)
        self.index: CodeIndex | None = None
        self.lsp_manager: LSPManager | None = None
        self._refresh_lock = asyncio.Lock()
        self.store = ExplorationStore(artifact_store)

    async def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.event_bus is not None:
            await self.event_bus.emit(name, source="codeintel", payload=payload)

    def _install_index(self, index: CodeIndex) -> None:
        self.index = index
        if self.lsp_manager is None:
            self.lsp_manager = LSPManager(
                self.workspace,
                index,
                providers=self.lsp_providers,
                event_bus=self.event_bus,
            )
        else:
            self.lsp_manager.index = index

    async def refresh(self, *, force_rebuild: bool = False) -> CodeIndex:
        async with self._refresh_lock:
            index, metrics = await asyncio.to_thread(
                self.indexer.build,
                force_rebuild=force_rebuild,
            )
            self._install_index(index)
        await self._emit("code_index.updated", metrics.model_dump(mode="json"))
        return index

    async def sync_written_file(self, path: Path | str) -> None:
        """Refresh the incremental index and best-effort sync one runtime write.

        The write has already committed when this callback runs.  Index or LSP
        failures are therefore observable but never reinterpret a successful
        file write as failed.
        """

        lexical = Path(path)
        if not lexical.is_absolute():
            lexical = self.workspace / lexical
        if lexical.is_symlink():
            await self._emit("code_index.write_sync_skipped", {"reason": "symlink"})
            return
        candidate = lexical.resolve(strict=False)
        try:
            relative = candidate.relative_to(self.workspace).as_posix()
        except ValueError:
            await self._emit("code_index.write_sync_skipped", {"reason": "outside_workspace"})
            return
        try:
            async with self._refresh_lock:
                # A first use can load an existing cache or start a small
                # targeted cache; it never needs a tree build merely because a
                # write callback ran.  A complete index remains available via
                # the explicit refresh/explore path.
                index, metrics = await asyncio.to_thread(
                    self.indexer.sync_file,
                    lexical,
                    index=self.index,
                )
                self._install_index(index)
            await self._emit("code_index.updated", metrics.model_dump(mode="json"))
            record = index.files.get(relative)
            if self.lsp_manager is None:
                return
            if record is None:
                state = self.lsp_manager.document_state(relative)
                if state is not None and state.open:
                    await self.lsp_manager.close_document(relative)
                await self._emit(
                    "code_index.write_sync_skipped",
                    {"path": relative, "reason": "removed_or_unsupported"},
                )
                return
            if record.language not in self.lsp_manager.providers:
                await self._emit(
                    "code_index.write_sync_skipped",
                    {"path": relative, "reason": "lsp_not_configured", "language": record.language},
                )
                return
            result = await self.lsp_manager.sync_saved_file(relative, language=record.language)
            await self._emit(
                "code_index.write_synced",
                {
                    "path": relative,
                    "language": record.language,
                    "version": result.state.version,
                    "lsp_synced": result.lsp_synced,
                    "fallback_used": not result.lsp_synced,
                },
            )
        except asyncio.CancelledError:
            # The workspace write completed before this callback was entered.
            # Do not let cancellation of a slow optional index/LSP operation
            # rewrite that successful write as a cancelled tool result.
            await self._emit(
                "code_index.write_sync_failed",
                {"path": relative, "error": "CancelledError"},
            )
        except Exception as error:
            await self._emit(
                "code_index.write_sync_failed",
                {"path": relative, "error": type(error).__name__},
            )

    def diagnostics(self, path: Path | str | None = None, *, limit: int = 100) -> tuple[DiagnosticRecord, ...]:
        """Return bounded diagnostics on demand; never inject them into prompts."""

        if self.lsp_manager is None:
            return ()
        return self.lsp_manager.diagnostics(path, limit=limit)

    async def _synthesize(self, report_context: str) -> tuple[str, str]:
        if self.model_client is None:
            return "", "model synthesis unavailable"
        budget = ContextBudget(self.model_client.capabilities)
        budget.set(BudgetCategory.RETRIEVED_CONTEXT, report_context)
        if not budget.is_within_budget:
            return "", "exploration context exceeds model budget"
        request = ModelRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Consolide o ExplorationReport em português. Use somente os componentes, fluxos, "
                        "unknowns e EvidenceRefs fornecidos. Não invente relações, não proponha mutações, "
                        "não exponha chain-of-thought e cite evidências como path:linha."
                    ),
                },
                {"role": "user", "content": report_context},
            ],
            temperature=self.temperature,
            max_tokens=6_000,
            reasoning_mode=ReasoningMode.DEEP,
        )
        try:
            response = await self.model_client.complete(request)
        except Exception as error:  # provider failure keeps deterministic report usable
            return "", str(error)[:1_000]
        return response.content.strip(), ""

    async def explore(
        self,
        objective: str,
        *,
        depth: ExplorationDepth = ExplorationDepth.DEEP,
        reuse: bool = True,
        synthesize: bool = True,
        source_budget_chars: int = 80_000,
        context_budget_tokens: int = 200_000,
    ) -> ExplorationRun:
        await self._emit("exploration.started", {"depth": depth.value})
        index = await self.refresh()
        reusable = self.store.find_reusable(objective, depth, index) if reuse else None
        if reusable is not None:
            report, metadata = reusable
            associated = metadata.metadata
            artifacts = ExplorationArtifacts(
                exploration_report=metadata.artifact_id,
                architecture_graph=str(associated["architecture_graph"]),
                execution_flow=str(associated["execution_flow"]),
                symbol_snapshot=str(associated["symbol_snapshot"]),
            )
            report = report.model_copy(
                update={"metrics": report.metrics.model_copy(update={"cache_hit": True})}
            )
            synthesis, synthesis_error = (
                await self._synthesize(report.compact_context()) if synthesize else ("", "")
            )
            await self._emit(
                "exploration.completed",
                {
                    "depth": depth.value,
                    "cache_hit": True,
                    "files_read": 0,
                    "symbols_inspected": report.metrics.symbols_inspected,
                    "relationships_found": report.metrics.relationships_found,
                    "duration_seconds": 0.0,
                },
            )
            return ExplorationRun(
                report=report,
                artifacts=artifacts,
                rendered=render_report(report),
                synthesis=synthesis,
                synthesis_error=synthesis_error,
                reused=True,
            )

        explorer = CodebaseExplorer(
            self.workspace,
            index,
            lsp_manager=self.lsp_manager,
            source_budget_chars=source_budget_chars,
            context_budget_tokens=context_budget_tokens,
        )
        report = await explorer.explore(objective, depth=depth)
        artifacts = self.store.persist(report)
        synthesis, synthesis_error = (
            await self._synthesize(report.compact_context()) if synthesize else ("", "")
        )
        await self._emit(
            "exploration.completed",
            {
                "depth": depth.value,
                "cache_hit": False,
                "files_read": report.metrics.files_actually_read,
                "symbols_inspected": report.metrics.symbols_inspected,
                "relationships_found": report.metrics.relationships_found,
                "duration_seconds": report.metrics.duration_seconds,
            },
        )
        return ExplorationRun(
            report=report,
            artifacts=artifacts,
            rendered=render_report(report),
            synthesis=synthesis,
            synthesis_error=synthesis_error,
        )

    async def prepare_for_workflow(
        self,
        objective: str,
        *,
        for_goal: bool,
        full_spec_workflow: bool = False,
        known_paths: tuple[str, ...] = (),
    ) -> ExplorationPreparation:
        decision = ExplorationAdmissionController().decide(
            objective,
            for_goal=for_goal,
            full_spec_workflow=full_spec_workflow,
            known_paths=known_paths,
        )
        if decision.level is ExplorationLevel.NONE:
            preparation = ExplorationPreparation(decision=decision)
            return preparation.model_copy(update={"directive": build_exploration_directive(preparation)})
        depth = ExplorationDepth.DEEP if decision.level is ExplorationLevel.DEEP else ExplorationDepth.TARGETED
        run = await self.explore(objective, depth=depth, reuse=True, synthesize=False)
        preparation = ExplorationPreparation(decision=decision, run=run)
        return preparation.model_copy(update={"directive": build_exploration_directive(preparation)})

    async def close(self) -> None:
        if self.lsp_manager is not None:
            await self.lsp_manager.shutdown()


__all__ = ["CodeIntelligenceRuntime"]
