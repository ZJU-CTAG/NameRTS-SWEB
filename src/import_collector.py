from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple, Union

from src.parser import TreeSitterClient

StrPath = Union[str, Path]
ResolvedImport = Tuple[Optional[str], Optional[str], Optional[str]]
_SOURCE_ROOT_MARKERS: Tuple[str, ...] = ("src", "source", "lib", "python", "py")


class ImportResolutionError(Exception):
    """Raised when an import statement cannot be interpreted."""


@dataclass
class ImportItem:
    """Represents a single target inside an import clause."""

    parts: List[str]
    alias: Optional[str]
    raw: str
    is_wildcard: bool = False

    @property
    def dotted_name(self) -> Optional[str]:
        if not self.parts:
            return None
        return ".".join(self.parts)


@dataclass
class ModuleRecord:
    """Represents a single module encountered while traversing a dotted path."""

    parts: Tuple[str, ...]
    path: Optional[Path]

    @property
    def module_name(self) -> str:
        return ".".join(self.parts)


def resolve_imports(
    project_root: StrPath,
    file_path: StrPath,
    from_clause: Optional[str],
    import_clause: str,
    as_clause: Optional[str] = None,
) -> List[ResolvedImport]:
    """
    Resolve the modules referenced by a Python import statement.

    Parameters
    ----------
    project_root:
        Root directory for the project. All returned file paths are relative to this path.
    file_path:
        Path to the module that contains the import statement (absolute or relative to the root).
    from_clause:
        Text inside the ``from`` field. Can include leading dots for relative imports.
    import_clause:
        Text inside the ``import`` field. May contain multiple targets separated by commas.
    as_clause:
        Optional text representing the collected ``as`` field when the parser being used
        exposes it separately. When present, it should contain the aliases in order,
        separated by commas. Any missing alias is treated as ``None``.

    Returns
    -------
    List[ResolvedImport]
        Tuples of ``(imported_file_path, imported_name, alias)`` where ``imported_file_path``
        is the relative path (using ``/`` separators) to the resolved module. If the target
        cannot be resolved inside ``project_root``, the path is ``None``. Parent packages
        that need to run their ``__init__`` while resolving a submodule are included so the
        caller can see every file that participates in the import.
    """

    root_path = _normalize_root(project_root)
    source_file = _normalize_source_file(root_path, file_path)
    if not import_clause or not import_clause.strip():
        raise ImportResolutionError("import_clause cannot be empty")

    import_items = _parse_import_clause(import_clause, as_clause)
    result: List[ResolvedImport] = []
    emitted: Set[ResolvedImport] = set()

    relative_source = source_file.relative_to(root_path)
    search_roots = _build_search_roots(root_path, relative_source)
    current_module_parts = _current_module_parts(root_path, relative_source)

    if from_clause:
        base_parts = _compute_from_base(current_module_parts, from_clause)
        base_chain = (
            _resolve_module_chain(root_path, base_parts, search_roots)
            if base_parts
            else []
        )
        result.extend(_emit_parent_packages(root_path, base_chain, emitted))
        for item in import_items:
            result.extend(
                _resolve_from_import_item(
                    root_path,
                    base_parts,
                    base_chain,
                    item,
                    emitted,
                    search_roots,
                )
            )
    else:
        for item in import_items:
            result.extend(
                _resolve_absolute_import_item(root_path, item, emitted, search_roots)
            )

    return result


def _normalize_root(path: StrPath) -> Path:
    root = Path(path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Project root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Project root must be a directory: {root}")
    return root


def _normalize_source_file(root: Path, file_path: StrPath) -> Path:
    raw_path = Path(file_path)
    candidate = raw_path if raw_path.is_absolute() else (root / raw_path)
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise ImportResolutionError(
            f"Source file {candidate} is not under project root {root}"
        ) from exc
    if not candidate.exists():
        raise FileNotFoundError(f"Source file not found: {candidate}")
    if candidate.is_dir():
        raise ImportResolutionError(f"Source path must point to a file: {candidate}")
    return candidate


def _current_module_parts(root: Path, relative_path: Path) -> List[str]:
    parent = relative_path.parent
    if str(parent) in {"", "."}:
        return []
    parts = list(parent.parts)
    accumulated: List[str] = []
    for index, part in enumerate(parts):
        accumulated.append(part)
        candidate = root.joinpath(*accumulated)
        if (candidate / "__init__.py").exists():
            return parts[index:]
    trim = 0
    while trim < len(parts) and parts[trim] in _SOURCE_ROOT_MARKERS:
        trim += 1
    return parts[trim:]


def _build_search_roots(root: Path, relative_path: Path) -> List[Path]:
    roots: List[Path] = []
    seen: Set[Path] = set()

    def _add(path: Path) -> None:
        if not path.exists() or not path.is_dir():
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        roots.append(path)

    _add(root)
    parent = relative_path.parent
    if str(parent) not in {"", "."}:
        accumulated: List[str] = []
        for part in parent.parts:
            accumulated.append(part)
            _add(root.joinpath(*accumulated))

    for name in _SOURCE_ROOT_MARKERS:
        _add(root / name)

    return roots


def _compute_from_base(current_parts: Sequence[str], from_clause: str) -> List[str]:
    stripped = from_clause.strip()
    dot_count = len(stripped) - len(stripped.lstrip("."))
    module_part = stripped[dot_count:]
    module_parts = [part for part in module_part.split(".") if part] if module_part else []
    if dot_count == 0:
        return module_parts
    if not current_parts:
        raise ImportResolutionError("Relative import outside of a package")
    if dot_count > len(current_parts):
        raise ImportResolutionError(
            f"Relative import beyond top-level package (dots={dot_count})"
        )
    levels_up = dot_count - 1
    cutoff = len(current_parts) - levels_up
    base = list(current_parts[:cutoff])
    base.extend(module_parts)
    return base


def _split_clause(text: Optional[str]) -> List[str]:
    if not text:
        return []
    flattened = text.replace("\\\n", " ").replace("\n", " ")
    flattened = flattened.replace("(", " ").replace(")", " ")
    tokens = [token.strip() for token in flattened.split(",")]
    return [token for token in tokens if token]


def _parse_import_clause(import_clause: str, alias_clause: Optional[str]) -> List[ImportItem]:
    raw_items = _split_clause(import_clause)
    if not raw_items:
        raise ImportResolutionError("No import targets were detected")
    alias_items = _split_clause(alias_clause)
    items: List[ImportItem] = []
    for index, token in enumerate(raw_items):
        alias_value: Optional[str] = None
        if " as " in token:
            before, after = token.split(" as ", 1)
            token = before.strip()
            alias_value = after.strip() or None
        if alias_value is None and index < len(alias_items):
            alias_candidate = alias_items[index].strip()
            alias_value = alias_candidate or None
        cleaned = token.strip()
        if not cleaned:
            continue
        if cleaned == "*":
            items.append(ImportItem(parts=[], alias=alias_value, raw="*", is_wildcard=True))
            continue
        parts = [part for part in cleaned.split(".") if part]
        if not parts:
            raise ImportResolutionError(f"Invalid import target: {token}")
        items.append(ImportItem(parts=parts, alias=alias_value, raw=cleaned))
    return items


def _resolve_module_chain(
    root: Path, parts: Sequence[str], search_roots: Sequence[Path]
) -> List[ModuleRecord]:
    if not parts:
        return []
    best: List[ModuleRecord] = []
    for base in search_roots:
        chain = _resolve_module_chain_from_base(base, parts)
        if not chain:
            continue
        if len(chain) == len(parts) and chain[-1].path is not None:
            return chain
        if len(chain) > len(best):
            best = chain
    return best


def _resolve_module_chain_from_base(root: Path, parts: Sequence[str]) -> List[ModuleRecord]:
    if not parts or not root.exists():
        return []
    current_dir = root
    collected: List[ModuleRecord] = []
    traversed: List[str] = []
    for index, part in enumerate(parts):
        traversed.append(part)
        is_last = index == len(parts) - 1
        package_dir = current_dir / part
        package_init = package_dir / "__init__.py"
        module_file = current_dir / f"{part}.py"

        if package_dir.is_dir() and package_init.exists():
            collected.append(ModuleRecord(parts=tuple(traversed), path=package_init))
            current_dir = package_dir
            continue

        if module_file.is_file():
            collected.append(ModuleRecord(parts=tuple(traversed), path=module_file))
            if not is_last:
                break
            continue

        if package_dir.is_dir():
            collected.append(ModuleRecord(parts=tuple(traversed), path=None))
            current_dir = package_dir
            continue

        collected.append(ModuleRecord(parts=tuple(traversed), path=None))
        break
    return collected


def _resolve_absolute_import_item(
    root: Path,
    item: ImportItem,
    emitted: Set[ResolvedImport],
    search_roots: Sequence[Path],
) -> List[ResolvedImport]:
    if item.is_wildcard:
        raise ImportResolutionError("Wildcard import is not valid without a from-clause")
    chain = _resolve_module_chain(root, item.parts, search_roots)
    if not chain:
        entry: ResolvedImport = (None, item.dotted_name, item.alias)
        return _maybe_add_entry(emitted, entry)
    return _emit_chain(
        root,
        chain,
        emitted,
        alias_for_last=item.alias,
        final_name_override=item.dotted_name,
    )


def _resolve_from_import_item(
    root: Path,
    base_parts: Sequence[str],
    base_chain: Sequence[ModuleRecord],
    item: ImportItem,
    emitted: Set[ResolvedImport],
    search_roots: Sequence[Path],
) -> List[ResolvedImport]:
    if item.is_wildcard:
        if not base_chain:
            raise ImportResolutionError("Wildcard import requires a concrete base module")
        base_path = base_chain[-1].path
        entry: ResolvedImport = (_relative_to_root(root, base_path), "*", None)
        return _maybe_add_entry(emitted, entry)

    combined = [*base_parts, *item.parts] if base_parts else list(item.parts)
    chain = _resolve_module_chain(root, combined, search_roots)
    expected_len = len(combined)
    chain_complete = len(chain) == expected_len and chain and chain[-1].path is not None
    skip = len(base_parts)

    if chain_complete:
        return _emit_chain(
            root,
            chain,
            emitted,
            alias_for_last=item.alias,
            final_name_override=item.parts[-1] if item.parts else item.raw,
            skip=skip,
        )

    base_record: Optional[ModuleRecord] = None
    if base_chain:
        base_record = base_chain[-1]
    elif chain:
        base_record = chain[-1]
    base_path = base_record.path if base_record else None
    entry: ResolvedImport = (
        _relative_to_root(root, base_path),
        item.parts[-1] if item.parts else item.raw,
        item.alias,
    )
    return _maybe_add_entry(emitted, entry)


def _relative_to_root(root: Path, target: Optional[Path]) -> Optional[str]:
    if target is None:
        return None
    try:
        return target.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - defensive guard
        return target.as_posix()


def _emit_chain(
    root: Path,
    chain: Sequence[ModuleRecord],
    emitted: Set[ResolvedImport],
    *,
    alias_for_last: Optional[str] = None,
    final_name_override: Optional[str] = None,
    skip: int = 0,
) -> List[ResolvedImport]:
    results: List[ResolvedImport] = []
    total = len(chain)
    for index in range(skip, total):
        record = chain[index]
        alias = alias_for_last if index == total - 1 else None
        imported_name = (
            final_name_override if index == total - 1 and final_name_override is not None else record.module_name
        )
        relative = _relative_to_root(root, record.path)
        if relative is None:
            continue
        entry: ResolvedImport = (relative, imported_name, alias)
        if entry in emitted:
            continue
        emitted.add(entry)
        results.append(entry)
    return results


def _emit_parent_packages(
    root: Path, chain: Sequence[ModuleRecord], emitted: Set[ResolvedImport]
) -> List[ResolvedImport]:
    results: List[ResolvedImport] = []
    for record in chain:
        path = record.path
        if path is None or path.name != "__init__.py":
            continue
        entry: ResolvedImport = (_relative_to_root(root, path), None, None)
        if entry in emitted:
            continue
        emitted.add(entry)
        results.append(entry)
    return results


def _maybe_add_entry(
    emitted: Set[ResolvedImport], entry: ResolvedImport
) -> List[ResolvedImport]:
    path, _, _ = entry
    if path is None:
        return []
    if entry in emitted:
        return []
    emitted.add(entry)
    return [entry]


@dataclass
class ImportStatement:
    from_clause: Optional[str]
    import_clause: str
    alias_clause: Optional[str]
    code: str
    lineno: int


def _collect_from_clause(node, source: str) -> Optional[str]:
    if node.type != "import_from_statement":
        return None
    module_node = node.child_by_field_name("module_name")
    if module_node is None:
        return None
    value = TreeSitterClient.retrieve_string_node(source, module_node).strip()
    return value or None


def _build_import_clause(node, source: str) -> str:
    parts: List[str] = []
    for idx, child in enumerate(node.children):
        field_name = node.field_name_for_child(idx)
        if field_name == "module_name":
            continue
        if child.type == "aliased_import":
            parts.append(TreeSitterClient.retrieve_string_node(source, child).strip())
        elif child.type == "dotted_name":
            if node.type == "import_from_statement" and field_name != "name":
                continue
            parts.append(TreeSitterClient.retrieve_string_node(source, child).strip())
        elif child.type == "wildcard_import":
            parts.append("*")
    return ", ".join(part for part in parts if part)


def extract_imports(file_path, file_contents, parsed_tree, project_path):
    capture = TreeSitterClient._capture_query_is(parsed_tree.root_node)
    nodes = capture.get("is", [])

    statements: List[ImportStatement] = []
    for node in nodes:
        import_clause = _build_import_clause(node, file_contents)
        if not import_clause:
            continue
        statements.append(
            ImportStatement(
                from_clause=_collect_from_clause(node, file_contents),
                import_clause=import_clause,
                alias_clause=None,
                code=TreeSitterClient.retrieve_string_node(file_contents, node),
                lineno=node.start_point[0] + 1,
            )
        )

    ret = list()
    for stmt in statements:
        try:
            resolved = resolve_imports(
                project_path,
                file_path,
                stmt.from_clause,
                stmt.import_clause,
                stmt.alias_clause,
            )
        except ImportResolutionError:
            # Repositories such as pylint intentionally keep syntactically
            # valid but semantically invalid relative imports in functional
            # test fixtures (for example, imports that traverse beyond the
            # package root).  Those files are analysis inputs rather than
            # importable project modules.  A single unresolvable statement
            # must not abort dependency collection for the entire checkout.
            continue
        ret.extend(resolved)
    return ret
