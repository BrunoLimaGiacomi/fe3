"""Static source analyzers with native parsers and optional Tree-sitter."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    Confidence,
    EvidenceRef,
    ImportRecord,
    ParseStatus,
    ReferenceRecord,
    RelationshipKind,
    RelationshipRecord,
    SymbolKind,
    SymbolRecord,
)


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".tf": "terraform",
    ".hcl": "hcl",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}


@dataclass(frozen=True, slots=True)
class ParseResult:
    symbols: tuple[SymbolRecord, ...] = ()
    imports: tuple[ImportRecord, ...] = ()
    references: tuple[ReferenceRecord, ...] = ()
    relationships: tuple[RelationshipRecord, ...] = ()
    status: ParseStatus = ParseStatus.PARSED
    source: str = ""
    error: str = ""


def language_for(path: Path) -> str:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "unknown")


def _symbol_id(path: str, qualified_name: str, line: int) -> str:
    raw = f"{path}:{qualified_name}:{line}".encode("utf-8")
    return "symbol-" + hashlib.sha256(raw).hexdigest()[:24]


def _evidence(path: str, symbol: str, node: ast.AST, source: str, confidence: Confidence) -> EvidenceRef:
    start = max(1, int(getattr(node, "lineno", 1)))
    end = max(start, int(getattr(node, "end_lineno", start)))
    return EvidenceRef(
        path=path,
        symbol=symbol,
        start_line=start,
        end_line=end,
        source=source,
        confidence=confidence,
    )


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, path: str, line_count: int) -> None:
        self.path = path
        self.source = "python_ast"
        module_name = Path(path).with_suffix("").as_posix().replace("/", ".")
        self.module_name = module_name
        self.scope: list[tuple[str, SymbolKind]] = [(module_name, SymbolKind.MODULE)]
        self.symbols: list[SymbolRecord] = [
            SymbolRecord(
                id=_symbol_id(path, module_name, 1),
                name=Path(path).stem,
                qualified_name=module_name,
                kind=SymbolKind.MODULE,
                path=path,
                start_line=1,
                end_line=max(1, line_count),
                source=self.source,
                exported=True,
            )
        ]
        self.imports: list[ImportRecord] = []
        self.references: list[ReferenceRecord] = []
        self.relationships: list[RelationshipRecord] = []

    @property
    def current(self) -> str:
        return self.scope[-1][0]

    def _qualified(self, name: str) -> str:
        return f"{self.current}.{name}"

    def _add_symbol(self, node: ast.AST, name: str, kind: SymbolKind, signature: str = "") -> str:
        qualified = self._qualified(name)
        start = max(1, int(getattr(node, "lineno", 1)))
        end = max(start, int(getattr(node, "end_lineno", start)))
        self.symbols.append(
            SymbolRecord(
                id=_symbol_id(self.path, qualified, start),
                name=name,
                qualified_name=qualified,
                kind=kind,
                path=self.path,
                start_line=start,
                end_line=end,
                source=self.source,
                signature=signature,
                exported=not name.startswith("_"),
            )
        )
        self.relationships.append(
            RelationshipRecord(
                source_symbol=self.current,
                target_symbol=qualified,
                kind=RelationshipKind.DEFINES,
                confidence=Confidence.CONFIRMED,
                evidence=(_evidence(self.path, qualified, node, self.source, Confidence.CONFIRMED),),
                origin=self.source,
            )
        )
        return qualified

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _PythonVisitor._call_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    @staticmethod
    def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        names = [argument.arg for argument in (*node.args.posonlyargs, *node.args.args)]
        if node.args.vararg:
            names.append("*" + node.args.vararg.arg)
        names.extend(argument.arg for argument in node.args.kwonlyargs)
        if node.args.kwarg:
            names.append("**" + node.args.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(names)})"

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self.imports.append(
                ImportRecord(
                    module=alias.name,
                    path=self.path,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    alias=alias.asname or "",
                    source=self.source,
                )
            )
            self.relationships.append(
                RelationshipRecord(
                    source_symbol=self.module_name,
                    target_symbol=alias.name,
                    kind=RelationshipKind.IMPORTS,
                    confidence=Confidence.CONFIRMED,
                    evidence=(_evidence(self.path, self.module_name, node, self.source, Confidence.CONFIRMED),),
                    origin=self.source,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            self.imports.append(
                ImportRecord(
                    module=module,
                    imported_name=alias.name,
                    path=self.path,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    alias=alias.asname or "",
                    source=self.source,
                )
            )
        self.relationships.append(
            RelationshipRecord(
                source_symbol=self.module_name,
                target_symbol=module,
                kind=RelationshipKind.IMPORTS,
                confidence=Confidence.CONFIRMED,
                evidence=(_evidence(self.path, self.module_name, node, self.source, Confidence.CONFIRMED),),
                origin=self.source,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        qualified = self._add_symbol(node, node.name, SymbolKind.CLASS)
        for base in node.bases:
            target = self._call_name(base)
            if target:
                self.relationships.append(
                    RelationshipRecord(
                        source_symbol=qualified,
                        target_symbol=target,
                        kind=RelationshipKind.INHERITS,
                        confidence=Confidence.CONFIRMED,
                        evidence=(_evidence(self.path, qualified, base, self.source, Confidence.CONFIRMED),),
                        origin=self.source,
                    )
                )
        self.scope.append((qualified, SymbolKind.CLASS))
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = SymbolKind.METHOD if self.scope[-1][1] is SymbolKind.CLASS else SymbolKind.FUNCTION
        qualified = self._add_symbol(node, node.name, kind, self._signature(node))
        self.scope.append((qualified, kind))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> Any:
        if self.scope[-1][1] in {SymbolKind.MODULE, SymbolKind.CLASS}:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    kind = SymbolKind.CONSTANT if target.id.isupper() else SymbolKind.VARIABLE
                    self._add_symbol(node, target.id, kind)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        if self.scope[-1][1] in {SymbolKind.MODULE, SymbolKind.CLASS} and isinstance(node.target, ast.Name):
            kind = SymbolKind.CONSTANT if node.target.id.isupper() else SymbolKind.VARIABLE
            self._add_symbol(node, node.target.id, kind)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.references.append(
                ReferenceRecord(
                    name=node.id,
                    path=self.path,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    context_symbol=self.current,
                    source=self.source,
                )
            )

    def visit_Call(self, node: ast.Call) -> Any:
        target = self._call_name(node.func)
        if target:
            self.relationships.append(
                RelationshipRecord(
                    source_symbol=self.current,
                    target_symbol=target,
                    kind=RelationshipKind.CALLS,
                    confidence=Confidence.INFERRED,
                    evidence=(_evidence(self.path, self.current, node, self.source, Confidence.INFERRED),),
                    origin=self.source,
                )
            )
        self.generic_visit(node)


class PythonAstAnalyzer:
    source = "python_ast"

    def parse(self, path: str, content: str) -> ParseResult:
        try:
            tree = ast.parse(content, filename=path, type_comments=True)
        except (SyntaxError, ValueError) as error:
            return ParseResult(status=ParseStatus.MALFORMED, source=self.source, error=str(error)[:1_000])
        visitor = _PythonVisitor(path, len(content.splitlines()))
        visitor.visit(tree)
        return ParseResult(
            symbols=tuple(visitor.symbols),
            imports=tuple(visitor.imports),
            references=tuple(visitor.references),
            relationships=tuple(visitor.relationships),
            source=self.source,
        )


class StructuredDataAnalyzer:
    def parse(self, path: str, content: str, language: str) -> ParseResult:
        source = f"{language}_native"
        try:
            if language == "json":
                value = json.loads(content)
            elif language == "toml":
                value = tomllib.loads(content)
            else:
                raise ValueError("unsupported structured language")
        except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as error:
            return ParseResult(status=ParseStatus.MALFORMED, source=source, error=str(error)[:1_000])
        module = Path(path).with_suffix("").as_posix().replace("/", ".")
        symbols = [
            SymbolRecord(
                id=_symbol_id(path, module, 1),
                name=Path(path).stem,
                qualified_name=module,
                kind=SymbolKind.MODULE,
                path=path,
                start_line=1,
                end_line=max(1, len(content.splitlines())),
                source=source,
                exported=True,
            )
        ]
        relationships: list[RelationshipRecord] = []
        if isinstance(value, dict):
            for key in value:
                name = str(key)
                qualified = f"{module}.{name}"
                symbols.append(
                    SymbolRecord(
                        id=_symbol_id(path, qualified, 1),
                        name=name,
                        qualified_name=qualified,
                        kind=SymbolKind.RESOURCE,
                        path=path,
                        start_line=1,
                        end_line=max(1, len(content.splitlines())),
                        source=source,
                        exported=True,
                    )
                )
                relationships.append(
                    RelationshipRecord(
                        source_symbol=module,
                        target_symbol=qualified,
                        kind=RelationshipKind.DEFINES,
                        confidence=Confidence.CONFIRMED,
                        evidence=(EvidenceRef(path=path, symbol=qualified, source=source),),
                        origin=source,
                    )
                )
        return ParseResult(symbols=tuple(symbols), relationships=tuple(relationships), source=source)


class TreeSitterAnalyzer:
    """Best-effort adapter; no grammar or language server is installed automatically."""

    _SOURCE = "tree_sitter"
    _MAX_DIAGNOSTIC_LENGTH = 1_000
    _NODE_KINDS = {
        "class_definition": SymbolKind.CLASS,
        "class_declaration": SymbolKind.CLASS,
        "function_definition": SymbolKind.FUNCTION,
        "function_declaration": SymbolKind.FUNCTION,
        "function_statement": SymbolKind.FUNCTION,
        "method_definition": SymbolKind.METHOD,
        "interface_declaration": SymbolKind.INTERFACE,
        "struct_item": SymbolKind.STRUCT,
        "enum_declaration": SymbolKind.ENUM,
        "resource": SymbolKind.RESOURCE,
    }

    def __init__(self) -> None:
        try:
            module = importlib.import_module("tree_sitter_language_pack")
            self._get_parser = getattr(module, "get_parser")
        except (ImportError, AttributeError):
            self._get_parser = None

    @property
    def available(self) -> bool:
        return self._get_parser is not None

    @staticmethod
    def _node_text(raw: bytes, node: Any) -> str:
        return raw[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @classmethod
    def _unquote(cls, value: str) -> str:
        """Remove source-language quote delimiters without damaging UTF-8 labels."""
        if len(value) < 2 or value[0] not in {"'", '"', "`"} or value[-1] != value[0]:
            return value
        body = value[1:-1]
        # Import paths and HCL labels commonly only need escaped delimiters and
        # backslashes handled here.  Do not run unicode_escape: it corrupts
        # already-decoded non-ASCII source text.
        quote = value[0]
        return body.replace("\\" + quote, quote).replace("\\\\", "\\")

    @classmethod
    def _name_node(cls, node: Any, kind: SymbolKind) -> Any | None:
        """Return a declaration name across the installed grammar variants."""
        for field in ("name", "function_name"):
            candidate = node.child_by_field_name(field)
            if candidate is not None:
                return candidate

        # The PowerShell grammar exposes function_name as a named child, not
        # as a ``name`` field.  Keep this constrained to declaration nodes so
        # arbitrary identifiers in a function body are never symbols.
        if str(node.type) == "function_statement":
            for candidate in node.named_children:
                if str(candidate.type) in {"function_name", "identifier", "command_name"}:
                    return candidate

        return None

    @classmethod
    def _hcl_name_node(cls, node: Any) -> Any | None:
        """Use the final quoted block label as an HCL/Terraform resource name."""
        labels = [candidate for candidate in node.named_children if str(candidate.type) == "string_lit"]
        return labels[-1] if labels else None

    @classmethod
    def _import_records(cls, path: str, node: Any, raw: bytes) -> list[ImportRecord]:
        source_node = node.child_by_field_name("source")
        if source_node is None:
            # Keep this tolerant of grammar revisions that omit the field but
            # still expose the module string as a direct named child.
            source_node = next(
                (candidate for candidate in node.named_children if str(candidate.type) in {"string", "string_lit"}),
                None,
            )
        if source_node is None:
            return []

        module = cls._unquote(cls._node_text(raw, source_node))
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        clause = next(
            (candidate for candidate in node.named_children if str(candidate.type) == "import_clause"),
            None,
        )
        bindings: list[tuple[str, str]] = []
        if clause is not None:
            for candidate in clause.named_children:
                candidate_type = str(candidate.type)
                if candidate_type == "identifier":
                    # ``import defaultName from 'module'``.
                    bindings.append(("default", cls._node_text(raw, candidate)))
                elif candidate_type == "namespace_import":
                    alias_node = next(
                        (item for item in candidate.named_children if str(item.type) in {"identifier", "type_identifier"}),
                        None,
                    )
                    bindings.append(("*", cls._node_text(raw, alias_node) if alias_node is not None else ""))
                elif candidate_type == "named_imports":
                    for specifier in candidate.named_children:
                        if str(specifier.type) != "import_specifier":
                            continue
                        imported_node = specifier.child_by_field_name("name")
                        if imported_node is None:
                            imported_node = next(iter(specifier.named_children), None)
                        alias_node = specifier.child_by_field_name("alias")
                        if imported_node is not None:
                            bindings.append(
                                (
                                    cls._node_text(raw, imported_node),
                                    cls._node_text(raw, alias_node) if alias_node is not None else "",
                                )
                            )

        # A side-effect-only import has no import_clause; retain one record for
        # the module so dependency searches still see it.
        if not bindings:
            bindings.append(("", ""))
        return [
            ImportRecord(
                module=module,
                imported_name=imported_name,
                alias=alias,
                path=path,
                start_line=start_line,
                end_line=end_line,
                source=cls._SOURCE,
            )
            for imported_name, alias in bindings
        ]

    @classmethod
    def _diagnostic(cls, root: Any, language: str) -> str:
        """Build a bounded actionable diagnostic from Tree-sitter error nodes."""
        problems: list[tuple[int, int, str, bool]] = []
        stack = [root]
        while stack:
            node = stack.pop()
            is_missing = bool(getattr(node, "is_missing", False))
            is_error = bool(getattr(node, "is_error", False)) or str(node.type) == "ERROR"
            if is_missing or is_error:
                row, column = node.start_point
                problems.append((row, column, str(node.type), is_missing))
            stack.extend(reversed(node.children))

        if problems:
            row, column, node_type, is_missing = min(problems)
            if is_missing:
                message = (
                    f"tree-sitter {language} syntax error at line {row + 1}, "
                    f"column {column + 1}: missing {node_type!r}"
                )
            else:
                message = (
                    f"tree-sitter {language} syntax error at line {row + 1}, "
                    f"column {column + 1}: unexpected {node_type!r}"
                )
            return message[: cls._MAX_DIAGNOSTIC_LENGTH]
        return f"tree-sitter {language} syntax error (error node reported)"[: cls._MAX_DIAGNOSTIC_LENGTH]

    def parse(self, path: str, content: str, language: str) -> ParseResult | None:
        if self._get_parser is None:
            return None
        grammar = "hcl" if language == "terraform" else language
        try:
            parser = self._get_parser(grammar)
            raw = content.encode("utf-8")
            tree = parser.parse(raw)
        except Exception as error:  # third-party grammar boundary
            return ParseResult(status=ParseStatus.FALLBACK, source="tree_sitter", error=str(error)[:1_000])
        symbols: list[SymbolRecord] = []
        imports: list[ImportRecord] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            node_type = str(node.type)
            if node_type == "import_statement" and language in {"javascript", "typescript"}:
                imports.extend(self._import_records(path, node, raw))

            kind = self._NODE_KINDS.get(node_type)
            name_node = self._hcl_name_node(node) if language in {"terraform", "hcl"} and node_type == "block" else None
            if name_node is not None:
                kind = SymbolKind.RESOURCE
            elif kind is not None:
                name_node = self._name_node(node, kind)
            if kind is not None:
                if name_node is not None:
                    name = self._unquote(self._node_text(raw, name_node))
                    qualified = f"{Path(path).stem}.{name}"
                    symbols.append(
                        SymbolRecord(
                            id=_symbol_id(path, qualified, node.start_point[0] + 1),
                            name=name,
                            qualified_name=qualified,
                            kind=kind,
                            path=path,
                            start_line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            source=self._SOURCE,
                            exported=not name.startswith("_"),
                        )
                    )
            stack.extend(reversed(node.children))
        malformed = bool(getattr(tree.root_node, "has_error", False))
        return ParseResult(
            symbols=tuple(symbols),
            imports=tuple(imports),
            status=ParseStatus.MALFORMED if malformed else ParseStatus.PARSED,
            source=self._SOURCE,
            error=self._diagnostic(tree.root_node, language) if malformed else "",
        )


class SourceAnalyzer:
    def __init__(self, *, tree_sitter: TreeSitterAnalyzer | None = None) -> None:
        self.python = PythonAstAnalyzer()
        self.structured = StructuredDataAnalyzer()
        self.tree_sitter = tree_sitter or TreeSitterAnalyzer()

    def parse(self, path: str, content: str, language: str) -> ParseResult:
        if language == "python":
            return self.python.parse(path, content)
        if language in {"json", "toml"}:
            return self.structured.parse(path, content, language)
        if language in {"bash", "powershell", "terraform", "hcl", "javascript", "typescript", "yaml"}:
            parsed = self.tree_sitter.parse(path, content, language)
            if parsed is not None:
                return parsed
            return ParseResult(status=ParseStatus.FALLBACK, source="lexical", error="tree-sitter unavailable")
        return ParseResult(status=ParseStatus.UNSUPPORTED, source="lexical", error="unsupported language")


__all__ = [
    "LANGUAGE_BY_SUFFIX",
    "ParseResult",
    "PythonAstAnalyzer",
    "SourceAnalyzer",
    "StructuredDataAnalyzer",
    "TreeSitterAnalyzer",
    "language_for",
]
