"""Automation for Python 3.6 scikit-learn SWE-bench validation.

The ground-truth command resets a scikit-learn worktree to one manifest base
commit, applies its golden patch, instruments the patched functions, and runs
each test file in its own pytest process.  The module intentionally supports
Python 3.6 as well as the current NameRTS Python versions.
"""

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.utils import collect_all_heuristic


HIT_FILE_ENV = "NAMERTS_GT_HIT_FILE"
DETAIL_FILE_ENV = "NAMERTS_GT_DETAIL_FILE"
DEFAULT_MANIFEST = "/shared_dir/sklearn_python36_golden_patches/manifest.tsv"
DEFAULT_REPO = "/shared_dir/swe-bench-repos/scikit-learn"
DEFAULT_OUTPUT = str(
    Path(__file__).resolve().parent.parent / "ground truth" / "gt_sklearn_python36.json"
)
DEFAULT_RUN_ROOT = str(
    Path(__file__).resolve().parent.parent / "runtime" / "sklearn_python36"
)
DEFAULT_BATCH_STATUS = str(
    Path(__file__).resolve().parent.parent
    / "runtime"
    / "sklearn_python36"
    / "batch_status.json"
)
DEFAULT_SEMANTIC_REVIEWS = str(
    Path(__file__).resolve().parent.parent
    / "ground truth"
    / "semantic_reviews_sklearn_python36.json"
)
DEFAULT_FINAL_SUMMARY = str(
    Path(__file__).resolve().parent.parent
    / "runtime"
    / "sklearn_python36"
    / "final_summary.json"
)


def _run(command, cwd=None, env=None, check=True, timeout=None):
    process = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
    )
    if check and process.returncode != 0:
        raise RuntimeError(
            "Command failed ({}): {}\nstdout:\n{}\nstderr:\n{}".format(
                process.returncode, " ".join(command), process.stdout, process.stderr
            )
        )
    return process


def load_manifest(path):
    manifest_path = Path(path).resolve()
    records = []
    with manifest_path.open("r", encoding="utf-8", newline="") as manifest_file:
        for row in csv.DictReader(manifest_file, delimiter="\t"):
            record = dict(row)
            record["patch_path"] = str((manifest_path.parent / row["patch_file"]).resolve())
            records.append(record)
    return records


def select_record(records, selector):
    matches = [
        record
        for record in records
        if selector in (record["instance_id"], record["base_commit"])
    ]
    if len(matches) != 1:
        raise ValueError("Expected one manifest record for {!r}, found {}".format(selector, len(matches)))
    return matches[0]


def parse_patch_changed_lines(patch_text):
    """Return old/new changed line numbers grouped by Python path."""
    changed = {}
    current_path = None
    old_line = None
    new_line = None
    hunk_pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for raw_line in patch_text.splitlines():
        if raw_line.startswith("+++ "):
            value = raw_line[4:].split("\t", 1)[0]
            if value == "/dev/null":
                current_path = None
            else:
                current_path = value[2:] if value.startswith("b/") else value
                if current_path.endswith(".py"):
                    changed.setdefault(current_path, {"old": set(), "new": set()})
                else:
                    current_path = None
            continue

        match = hunk_pattern.match(raw_line)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            continue
        if current_path is None or old_line is None or new_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            changed[current_path]["new"].add(new_line)
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            changed[current_path]["old"].add(old_line)
            old_line += 1
        elif raw_line.startswith("\\ No newline"):
            continue
        else:
            old_line += 1
            new_line += 1
    return changed


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self):
        self.stack = []
        self.functions = []

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node):
        qualname = ".".join(self.stack + [node.name])
        start_lines = [node.lineno]
        start_lines.extend(decorator.lineno for decorator in node.decorator_list)
        descendant_lines = [
            child.lineno for child in ast.walk(node) if hasattr(child, "lineno")
        ]
        end_line = max(descendant_lines or [node.lineno])
        self.functions.append(
            {
                "qualname": qualname,
                "node": node,
                "start_line": min(start_lines),
                "end_line": end_line,
            }
        )
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)


def collect_functions(source, filename):
    tree = ast.parse(source, filename=filename)
    collector = _FunctionCollector()
    collector.visit(tree)
    return collector.functions


def functions_for_lines(functions, changed_lines):
    selected = {}
    for line_number in sorted(changed_lines):
        containing = [
            function
            for function in functions
            if function["start_line"] <= line_number <= function["end_line"]
        ]
        if not containing:
            continue
        function = min(
            containing,
            key=lambda item: (item["end_line"] - item["start_line"], -item["start_line"]),
        )
        selected[function["qualname"]] = function
    return selected


def _is_docstring_statement(node):
    if not isinstance(node, ast.Expr):
        return False
    value = node.value
    if isinstance(value, ast.Str):
        return True
    constant_type = getattr(ast, "Constant", None)
    return constant_type is not None and isinstance(value, constant_type) and isinstance(value.value, str)


def insertion_line(function):
    body = function["node"].body
    if not body:
        raise ValueError("Cannot instrument empty function {}".format(function["qualname"]))
    if _is_docstring_statement(body[0]) and len(body) > 1:
        return body[1].lineno
    return body[0].lineno


def module_insertion_line(source, filename):
    """Return a legal line for a module probe, preserving future imports."""
    tree = ast.parse(source, filename=filename)
    body = list(tree.body)
    index = 0
    if body and _is_docstring_statement(body[0]):
        index += 1
    while (
        index < len(body)
        and isinstance(body[index], ast.ImportFrom)
        and body[index].module == "__future__"
    ):
        index += 1
    if index < len(body):
        return body[index].lineno
    return len(source.splitlines()) + 1


def source_at_commit(repo, commit, relative_path):
    result = _run(["git", "show", "{}:{}".format(commit, relative_path)], cwd=repo)
    return result.stdout


def identify_modified_functions(repo, base_commit, patch_text, changed_lines):
    result = []
    uninstrumentable = []
    for relative_path, line_sets in sorted(changed_lines.items()):
        patched_path = Path(repo) / relative_path
        if not patched_path.exists():
            uninstrumentable.append(
                {"path": relative_path, "reason": "patched Python file was deleted"}
            )
            continue

        patched_source = patched_path.read_text(encoding="utf-8")
        patched_functions = collect_functions(patched_source, relative_path)
        patched_by_name = {item["qualname"]: item for item in patched_functions}
        selected = functions_for_lines(patched_functions, line_sets["new"])

        try:
            old_source = source_at_commit(repo, base_commit, relative_path)
        except RuntimeError:
            old_source = None
        if old_source is not None:
            old_functions = collect_functions(old_source, relative_path)
            old_selected = functions_for_lines(old_functions, line_sets["old"])
            for qualname in old_selected:
                if qualname in patched_by_name:
                    selected.setdefault(qualname, patched_by_name[qualname])
                else:
                    uninstrumentable.append(
                        {
                            "path": relative_path,
                            "qualname": qualname,
                            "reason": "modified function is absent after patch",
                        }
                    )

        patched_function_lines = set()
        for function in patched_functions:
            patched_function_lines.update(
                range(function["start_line"], function["end_line"] + 1)
            )
        module_level_changed = bool(
            set(line_sets["new"]) - patched_function_lines
        )
        # A module probe is the fallback for a purely module-level patch.
        # When functions in the same file are modified, probing every import
        # turns ordinary support-import changes into a project-wide false
        # ground truth.
        if module_level_changed and not selected:
            selected["<module>"] = {
                "node": ast.parse(patched_source, filename=relative_path),
                "qualname": "<module>",
                "start_line": 1,
                "end_line": len(patched_source.splitlines()),
            }
        for qualname, function in sorted(selected.items()):
            insertion = (
                module_insertion_line(patched_source, relative_path)
                if qualname == "<module>"
                else insertion_line(function)
            )
            result.append(
                {
                    "path": relative_path,
                    "qualname": qualname,
                    "marker": "{}::{}".format(relative_path, qualname),
                    "line": getattr(function["node"], "lineno", 1),
                    "insertion_line": insertion,
                }
            )
    return result, uninstrumentable


def instrument_functions(repo, functions):
    grouped = {}
    for function in functions:
        grouped.setdefault(function["path"], []).append(function)

    for relative_path, path_functions in grouped.items():
        file_path = Path(repo) / relative_path
        source_lines = file_path.read_text(encoding="utf-8").splitlines(True)
        insertions = []
        for function in path_functions:
            line_index = function["insertion_line"] - 1
            existing_line = source_lines[line_index]
            leading = existing_line[: len(existing_line) - len(existing_line.lstrip())]
            marker_value = function["marker"]
            marker = repr(marker_value + "\n")
            detail_prefix = repr(marker_value + "\t")
            probe = (
                "{indent}__import__('os').environ.get('{env_name}') and "
                "open(__import__('os').environ['{env_name}'], 'a').write({marker})\n"
                "{indent}__import__('os').environ.get('{detail_env}') and "
                "open(__import__('os').environ['{detail_env}'], 'a').write("
                "{detail_prefix} + __import__('os').environ.get("
                "'PYTEST_CURRENT_TEST', '<outside-test>') + '\\n')\n"
            ).format(
                indent=leading,
                env_name=HIT_FILE_ENV,
                marker=marker,
                detail_env=DETAIL_FILE_ENV,
                detail_prefix=detail_prefix,
            )
            insertions.append((line_index, probe))
        for line_index, probe in sorted(insertions, reverse=True):
            source_lines.insert(line_index, probe)
        file_path.write_text("".join(source_lines), encoding="utf-8")


def resolve_conda_python(env_name):
    result = _run(
        ["conda", "run", "-n", env_name, "python", "-c", "import sys; print(sys.executable)"]
    )
    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not candidates:
        raise RuntimeError("Could not resolve Python executable for conda env {}".format(env_name))
    executable = Path(candidates[-1])
    if not executable.exists():
        raise RuntimeError("Resolved test Python does not exist: {}".format(executable))
    return str(executable)


def _safe_test_id(test_file):
    digest = hashlib.sha1(test_file.encode("utf-8")).hexdigest()[:12]
    return "{}_{}".format(digest, Path(test_file).name)


def run_one_test(test_python, repo, test_file, run_dir, timeout):
    safe_id = _safe_test_id(test_file)
    hit_path = run_dir / "hits" / (safe_id + ".txt")
    detail_path = run_dir / "details" / (safe_id + ".txt")
    log_path = run_dir / "logs" / (safe_id + ".log")
    environment = os.environ.copy()
    environment[HIT_FILE_ENV] = str(hit_path)
    environment[DETAIL_FILE_ENV] = str(detail_path)
    environment["PYTHONHASHSEED"] = "0"
    environment["OMP_NUM_THREADS"] = "1"
    environment["OPENBLAS_NUM_THREADS"] = "1"
    environment["MKL_NUM_THREADS"] = "1"
    command = [test_python, "-m", "pytest", "-q", test_file, "-p", "no:cacheprovider"]
    started = time.time()
    timed_out = False
    try:
        process = _run(command, cwd=repo, env=environment, check=False, timeout=timeout)
        return_code = process.returncode
        output = "stdout:\n{}\n\nstderr:\n{}".format(process.stdout, process.stderr)
    except subprocess.TimeoutExpired as error:
        timed_out = True
        return_code = -1
        stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        output = "TIMEOUT after {} seconds\nstdout:\n{}\n\nstderr:\n{}".format(timeout, stdout, stderr)
    log_path.write_text(output, encoding="utf-8")
    markers = []
    if hit_path.exists():
        markers = sorted(set(hit_path.read_text(encoding="utf-8").splitlines()))
    details = []
    if detail_path.exists():
        details = sorted(set(detail_path.read_text(encoding="utf-8").splitlines()))
    return {
        "test_file": test_file,
        "return_code": return_code,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 3),
        "markers": markers,
        "details": details,
        "log": str(log_path),
    }


def run_tests(test_python, repo, test_files, run_dir, workers, timeout):
    results = []
    total = len(test_files)
    with ThreadPoolExecutor(max_workers=min(workers, total or 1)) as executor:
        futures = {
            executor.submit(run_one_test, test_python, repo, test_file, run_dir, timeout): test_file
            for test_file in test_files
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if index == total or index % 10 == 0:
                print("Ground truth progress: {}/{}".format(index, total), flush=True)
    return sorted(results, key=lambda item: item["test_file"])


def update_ground_truth(output_path, record):
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        with path.open("r", encoding="utf-8") as output_file:
            existing = json.load(output_file)
        if not isinstance(existing, list):
            raise ValueError("Ground-truth output must contain a JSON array: {}".format(path))
    existing = [item for item in existing if item.get("instance_id") != record["instance_id"]]
    existing.append(record)
    existing.sort(key=lambda item: item["instance_id"])
    with path.open("w", encoding="utf-8") as output_file:
        json.dump(existing, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def assert_clean_tracked_worktree(repo):
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo
    ).stdout.strip()
    if status:
        raise RuntimeError("Refusing to reset a worktree with tracked changes:\n{}".format(status))


def clean_and_build_extensions(repo, test_env, build_workers, log_path):
    """Rebuild generated extensions for the exact checked-out scikit-learn commit."""
    tracked_extensions = _run(
        ["git", "ls-files", "sklearn/*.so", "sklearn/**/*.so"], cwd=repo
    ).stdout.strip()
    if tracked_extensions:
        raise RuntimeError(
            "Refusing to remove tracked extension files:\n{}".format(tracked_extensions)
        )

    sklearn_root = Path(repo) / "sklearn"
    removed = []
    for pattern in ("*.so", "*.pyc"):
        for artifact in sklearn_root.rglob(pattern):
            artifact.unlink()
            removed.append(str(artifact.relative_to(repo)))
    for cache_dir in sorted(
        sklearn_root.rglob("__pycache__"),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            cache_dir.rmdir()
        except OSError:
            pass

    test_python = resolve_conda_python(test_env)
    started = time.time()
    build_result = _run(
        [
            test_python,
            "setup.py",
            "build_ext",
            "-i",
            "-f",
            "-j",
            str(max(1, build_workers)),
        ],
        cwd=repo,
        check=False,
    )
    elapsed = round(time.time() - started, 3)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "removed_generated_artifacts={}\nreturn_code={}\nelapsed_seconds={}\n\n"
        "stdout:\n{}\n\nstderr:\n{}".format(
            len(removed),
            build_result.returncode,
            elapsed,
            build_result.stdout,
            build_result.stderr,
        ),
        encoding="utf-8",
    )
    if build_result.returncode != 0:
        raise RuntimeError(
            "Extension build failed with exit {}. See {}".format(
                build_result.returncode, log_path
            )
        )
    return {
        "elapsed_seconds": elapsed,
        "log": str(log_path),
        "removed_generated_artifacts": len(removed),
    }


def _load_json_list(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as input_file:
        value = json.load(input_file)
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array: {}".format(path))
    return value


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output_file:
        json.dump(value, output_file, indent=2, sort_keys=True)
        output_file.write("\n")


def ground_truth(args):
    repo = Path(args.repo).resolve()
    records = load_manifest(args.manifest)
    record = select_record(records, args.instance)
    patch_path = Path(record["patch_path"])
    patch_text = patch_path.read_text(encoding="utf-8")
    changed_lines = parse_patch_changed_lines(patch_text)

    assert_clean_tracked_worktree(repo)
    _run(["git", "reset", "--hard", record["base_commit"]], cwd=repo)
    _run(["git", "apply", "--check", str(patch_path)], cwd=repo)
    _run(["git", "apply", str(patch_path)], cwd=repo)

    functions, uninstrumentable = identify_modified_functions(
        repo, record["base_commit"], patch_text, changed_lines
    )
    if not functions:
        raise RuntimeError("Patch has no instrumentable Python functions: {}".format(patch_path))
    instrument_functions(repo, functions)

    run_id = "{}_{}_{}".format(record["instance_id"], int(time.time()), os.getpid())
    run_dir = Path(args.run_root).resolve() / run_id
    (run_dir / "hits").mkdir(parents=True)
    (run_dir / "details").mkdir(parents=True)
    (run_dir / "logs").mkdir(parents=True)
    with (run_dir / "instrumentation.json").open("w", encoding="utf-8") as metadata_file:
        json.dump(
            {"functions": functions, "uninstrumentable": uninstrumentable},
            metadata_file,
            indent=2,
            sort_keys=True,
        )

    test_files = sorted(collect_all_heuristic(str(repo)))
    test_python = resolve_conda_python(args.test_env)
    version_result = _run([test_python, "--version"])
    test_python_version = (version_result.stdout or version_result.stderr).strip()
    print(
        "Running {} test files with {} workers under {}".format(
            len(test_files), args.workers, test_python_version
        ),
        flush=True,
    )
    started = time.time()
    test_results = run_tests(
        test_python, repo, test_files, run_dir, args.workers, args.timeout
    )
    affected = sorted(item["test_file"] for item in test_results if item["markers"])
    failed = sorted(
        item["test_file"]
        for item in test_results
        if item["return_code"] != 0 and not item["timed_out"]
    )
    timed_out = sorted(item["test_file"] for item in test_results if item["timed_out"])
    affected_functions = {
        item["test_file"]: item["markers"] for item in test_results if item["markers"]
    }
    result_record = {
        "base_commit": record["base_commit"],
        "instance_id": record["instance_id"],
        "version": record["version"],
        "patch_file": record["patch_file"],
        "python": test_python_version,
        "modified_functions": functions,
        "uninstrumentable_changes": uninstrumentable,
        "tests_to_run": affected,
        "affected_functions_by_test": affected_functions,
        "total_tests": len(test_files),
        "passed_test_files": len(test_files) - len(failed) - len(timed_out),
        "failed_test_files": failed,
        "timed_out_test_files": timed_out,
        "elapsed_seconds": round(time.time() - started, 3),
        "run_dir": str(run_dir),
    }
    update_ground_truth(args.output, result_record)
    with (run_dir / "test_results.json").open("w", encoding="utf-8") as results_file:
        json.dump(test_results, results_file, indent=2, sort_keys=True)
    if not args.keep_worktree:
        _run(["git", "reset", "--hard", record["base_commit"]], cwd=repo)
    print(json.dumps(result_record, indent=2, sort_keys=True))
    return 0


def trace_instance(args):
    """Trace modified-function calls back to individual pytest node ids."""
    repo = Path(args.repo).resolve()
    records = load_manifest(args.manifest)
    record = select_record(records, args.instance)
    patch_path = Path(record["patch_path"])
    patch_text = patch_path.read_text(encoding="utf-8")
    changed_lines = parse_patch_changed_lines(patch_text)

    assert_clean_tracked_worktree(repo)
    _run(["git", "reset", "--hard", record["base_commit"]], cwd=repo)
    build_log = (
        Path(args.run_root).resolve()
        / "builds"
        / ("trace_" + record["instance_id"] + ".log")
    )
    clean_and_build_extensions(repo, args.test_env, args.build_workers, build_log)
    _run(["git", "apply", "--check", str(patch_path)], cwd=repo)
    _run(["git", "apply", str(patch_path)], cwd=repo)
    functions, uninstrumentable = identify_modified_functions(
        repo, record["base_commit"], patch_text, changed_lines
    )
    if not functions:
        raise RuntimeError("Patch has no instrumentable Python functions: {}".format(patch_path))
    instrument_functions(repo, functions)

    run_id = "trace_{}_{}_{}".format(record["instance_id"], int(time.time()), os.getpid())
    run_dir = Path(args.run_root).resolve() / run_id
    (run_dir / "hits").mkdir(parents=True)
    (run_dir / "details").mkdir(parents=True)
    (run_dir / "logs").mkdir(parents=True)
    test_python = resolve_conda_python(args.test_env)
    try:
        test_results = run_tests(
            test_python,
            repo,
            sorted(set(args.test_files)),
            run_dir,
            args.workers,
            args.timeout,
        )
        result = {
            "base_commit": record["base_commit"],
            "instance_id": record["instance_id"],
            "modified_functions": functions,
            "uninstrumentable_changes": uninstrumentable,
            "test_results": test_results,
            "run_dir": str(run_dir),
        }
        _write_json(run_dir / "trace.json", result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        if not args.keep_worktree:
            _run(["git", "reset", "--hard", record["base_commit"]], cwd=repo)


def apply_semantic_reviews(args):
    """Apply human-reviewed include/exclude decisions to dynamic hit candidates."""
    records = _load_json_list(args.ground_truth)
    reviews = _load_json_list(args.reviews)
    records_by_instance = {
        record["instance_id"]: record for record in records
    }
    for review in reviews:
        instance_id = review["instance_id"]
        if instance_id not in records_by_instance:
            raise ValueError("Review references unknown ground truth: {}".format(instance_id))
        if review["decision"] not in ("retain", "exclude"):
            raise ValueError("Invalid semantic decision: {}".format(review["decision"]))

    for record in records:
        dynamic_hits = record.get("dynamic_hit_tests")
        if dynamic_hits is None:
            dynamic_hits = list(record.get("tests_to_run", []))
        dynamic_hits = sorted(set(dynamic_hits))
        instance_reviews = [
            review for review in reviews
            if review["instance_id"] == record["instance_id"]
        ]
        final_tests = set(dynamic_hits)
        for review in instance_reviews:
            test_file = review["test_file"]
            if test_file not in dynamic_hits:
                raise ValueError(
                    "Review test {} is not a dynamic hit for {}".format(
                        test_file, record["instance_id"]
                    )
                )
            if review["decision"] == "exclude":
                final_tests.discard(test_file)
        record["dynamic_hit_tests"] = dynamic_hits
        record["semantic_reviews"] = instance_reviews
        record["tests_to_run"] = sorted(final_tests)

    _write_json(args.ground_truth, sorted(records, key=lambda item: item["instance_id"]))
    print(
        "Applied {} semantic reviews to {} ground-truth records".format(
            len(reviews), len(records)
        )
    )
    return 0


def summarize_results(args):
    """Combine reviewed ground truth with the latest NameRTS result per instance."""
    records = _load_json_list(args.ground_truth)
    run_root = Path(args.run_root).resolve()
    instances = []
    for record in records:
        comparison_paths = list(
            run_root.glob("namerts_{}_*.json".format(record["instance_id"]))
        )
        if not comparison_paths:
            raise RuntimeError(
                "No NameRTS comparison found for {}".format(record["instance_id"])
            )
        comparison_path = max(
            comparison_paths, key=lambda path: (path.stat().st_mtime, path.name)
        )
        with comparison_path.open("r", encoding="utf-8") as comparison_file:
            comparison = json.load(comparison_file)
        selected = sorted(set(comparison["tests_to_run"]))
        final_ground_truth = sorted(set(record["tests_to_run"]))
        dynamic_hits = sorted(
            set(record.get("dynamic_hit_tests", final_ground_truth))
        )
        missing = sorted(set(final_ground_truth) - set(selected))
        instances.append(
            {
                "base_commit": record["base_commit"],
                "comparison": str(comparison_path),
                "dynamic_hit_tests": dynamic_hits,
                "failed_test_files": record["failed_test_files"],
                "final_ground_truth": final_ground_truth,
                "instance_id": record["instance_id"],
                "missing_tests": missing,
                "passed_test_files": record["passed_test_files"],
                "safe": not missing,
                "semantic_reviews": record.get("semantic_reviews", []),
                "tests_to_run": selected,
                "timed_out_test_files": record["timed_out_test_files"],
                "total_test_files": record["total_tests"],
                "version": record["version"],
            }
        )

    summary = {
        "aggregate": {
            "dynamic_hit_test_files": sum(
                len(item["dynamic_hit_tests"]) for item in instances
            ),
            "failed_test_file_runs": sum(
                len(item["failed_test_files"]) for item in instances
            ),
            "final_ground_truth_test_files": sum(
                len(item["final_ground_truth"]) for item in instances
            ),
            "instances": len(instances),
            "passed_test_file_runs": sum(
                item["passed_test_files"] for item in instances
            ),
            "safe_instances": sum(1 for item in instances if item["safe"]),
            "selected_test_files": sum(
                len(item["tests_to_run"]) for item in instances
            ),
            "timed_out_test_file_runs": sum(
                len(item["timed_out_test_files"]) for item in instances
            ),
            "total_test_file_runs": sum(
                item["total_test_files"] for item in instances
            ),
            "unsafe_instances": sum(1 for item in instances if not item["safe"]),
        },
        "instances": sorted(instances, key=lambda item: item["instance_id"]),
    }
    _write_json(args.output, summary)
    print(json.dumps(summary["aggregate"], indent=2, sort_keys=True))
    return 0


def namerts(args):
    from src.evaluate import CACHED_FILES_NBDP, evaluate_instance, install_instrumentor
    from src.namebdp import NameBDP

    repo = Path(args.repo).resolve()
    records = load_manifest(args.manifest)
    record = select_record(records, args.instance)
    patch_path = Path(record["patch_path"]).resolve()

    ground_truth_path = Path(args.ground_truth).resolve()
    if not ground_truth_path.exists():
        raise RuntimeError("Ground truth does not exist: {}".format(ground_truth_path))
    with ground_truth_path.open("r", encoding="utf-8") as ground_truth_file:
        ground_truth_records = json.load(ground_truth_file)
    matching_ground_truth = [
        item for item in ground_truth_records if item.get("instance_id") == record["instance_id"]
    ]
    if len(matching_ground_truth) != 1:
        raise RuntimeError(
            "Expected one ground-truth record for {}, found {}".format(
                record["instance_id"], len(matching_ground_truth)
            )
        )

    install_instrumentor(args.test_env)
    result = evaluate_instance(
        direct_parent=record["base_commit"],
        true_parent=record["base_commit"],
        current=None,
        target=True,
        tool_name="NameBDP",
        repo_path=str(repo),
        conda_env=args.test_env,
        tool_class=NameBDP,
        cached_files=CACHED_FILES_NBDP,
        time_tag=str(time.time()),
        n=args.workers,
        registry_decorator_keywords=set(),
        use_isolation=False,
        run_parent=True,
        patch_path=str(patch_path),
        run_current_tests=False,
        reuse_parent_cache=args.reuse_parent_cache,
    )

    selected = {Path(path).as_posix() for path in result["tests_to_run"]}
    affected = {
        Path(path).as_posix() for path in matching_ground_truth[0]["tests_to_run"]
    }
    missing = sorted(affected - selected)
    comparison = {
        "instance_id": record["instance_id"],
        "base_commit": record["base_commit"],
        "patch_file": record["patch_file"],
        "ground_truth": sorted(affected),
        "tests_to_run": sorted(selected),
        "missing_tests": missing,
        "safe": not missing,
        "namerts_result": result,
    }
    output_root = Path(args.run_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "namerts_{}_{}.json".format(
        record["instance_id"], int(time.time())
    )
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(comparison, output_file, indent=2, sort_keys=True)
        output_file.write("\n")
    if not args.keep_worktree:
        _run(["git", "reset", "--hard", record["base_commit"]], cwd=repo)
    comparison["output"] = str(output_path)
    print(json.dumps(comparison, indent=2, sort_keys=True))
    return 0 if not missing else 1


def batch(args):
    """Run rebuild, dynamic ground truth, and NameRTS comparison per instance."""
    repo = Path(args.repo).resolve()
    run_root = Path(args.run_root).resolve()
    records = load_manifest(args.manifest)
    requested = set(args.instances or [])
    if requested:
        known = {
            item
            for record in records
            for item in (record["instance_id"], record["base_commit"])
        }
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError("Unknown instance selectors: {}".format(unknown))
        records = [
            record
            for record in records
            if record["instance_id"] in requested or record["base_commit"] in requested
        ]

    ground_truth_records = _load_json_list(args.ground_truth)
    completed_ground_truth = {
        record["instance_id"] for record in ground_truth_records
    }
    status_path = Path(args.status).resolve()
    existing_status = _load_json_list(status_path)
    status_by_instance = {
        item["instance_id"]: item for item in existing_status
    }

    for index, record in enumerate(records, start=1):
        instance_id = record["instance_id"]
        comparison_pattern = "namerts_{}_*.json".format(instance_id)
        comparisons = sorted(run_root.glob(comparison_pattern))
        need_ground_truth = (
            args.force_ground_truth or instance_id not in completed_ground_truth
        )
        need_namerts = args.force_namerts or not comparisons
        if args.ground_truth_only:
            need_namerts = False
        if args.namerts_only:
            need_ground_truth = False

        state = status_by_instance.setdefault(
            instance_id,
            {
                "base_commit": record["base_commit"],
                "instance_id": instance_id,
                "version": record["version"],
            },
        )
        state["batch_index"] = index
        state["batch_total"] = len(records)
        if not need_ground_truth and not need_namerts:
            state["status"] = "skipped_completed"
            _write_json(status_path, sorted(status_by_instance.values(), key=lambda x: x["instance_id"]))
            print(
                "Batch {}/{}: {} already complete".format(index, len(records), instance_id),
                flush=True,
            )
            continue

        print(
            "Batch {}/{}: preparing {}".format(index, len(records), instance_id),
            flush=True,
        )
        try:
            assert_clean_tracked_worktree(repo)
            _run(["git", "reset", "--hard", record["base_commit"]], cwd=repo)
            build_log = run_root / "builds" / (instance_id + ".log")
            state["build"] = clean_and_build_extensions(
                repo, args.test_env, args.build_workers, build_log
            )

            if need_ground_truth:
                gt_args = argparse.Namespace(
                    instance=instance_id,
                    manifest=args.manifest,
                    repo=str(repo),
                    test_env=args.test_env,
                    output=args.ground_truth,
                    run_root=str(run_root),
                    workers=args.ground_truth_workers,
                    timeout=args.timeout,
                    keep_worktree=False,
                )
                state["ground_truth_return_code"] = ground_truth(gt_args)
                completed_ground_truth.add(instance_id)

            if need_namerts:
                namerts_args = argparse.Namespace(
                    instance=instance_id,
                    manifest=args.manifest,
                    repo=str(repo),
                    test_env=args.test_env,
                    ground_truth=args.ground_truth,
                    run_root=str(run_root),
                    workers=args.namerts_workers,
                    keep_worktree=False,
                    reuse_parent_cache=args.reuse_parent_cache,
                )
                state["namerts_return_code"] = namerts(namerts_args)

            state["status"] = "completed"
            state.pop("error", None)
        except Exception as error:
            state["status"] = "failed"
            state["error"] = "{}: {}".format(type(error).__name__, error)
            print(
                "Batch failure for {}: {}".format(instance_id, state["error"]),
                file=sys.stderr,
                flush=True,
            )
            if not args.continue_on_error:
                _write_json(
                    status_path,
                    sorted(status_by_instance.values(), key=lambda x: x["instance_id"]),
                )
                raise
        finally:
            try:
                _run(["git", "reset", "--hard", record["base_commit"]], cwd=repo)
            except Exception as reset_error:
                state["reset_error"] = str(reset_error)
            _write_json(
                status_path,
                sorted(status_by_instance.values(), key=lambda x: x["instance_id"]),
            )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    ground_truth_parser = subparsers.add_parser(
        "ground-truth", help="Collect dynamically affected test files for one instance"
    )
    ground_truth_parser.add_argument("--instance", required=True, help="Instance id or base commit")
    ground_truth_parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ground_truth_parser.add_argument("--repo", default=DEFAULT_REPO)
    ground_truth_parser.add_argument("--test-env", default="RTSTest_SC36")
    ground_truth_parser.add_argument("--output", default=DEFAULT_OUTPUT)
    ground_truth_parser.add_argument("--run-root", default=DEFAULT_RUN_ROOT)
    ground_truth_parser.add_argument("--workers", type=int, default=20)
    ground_truth_parser.add_argument("--timeout", type=int, default=900)
    ground_truth_parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help="Leave the applied patch and instrumentation in the worktree for debugging",
    )
    ground_truth_parser.set_defaults(handler=ground_truth)

    namerts_parser = subparsers.add_parser(
        "namerts", help="Build a parent cache, select patched tests, and compare with ground truth"
    )
    namerts_parser.add_argument("--instance", required=True, help="Instance id or base commit")
    namerts_parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    namerts_parser.add_argument("--repo", default=DEFAULT_REPO)
    namerts_parser.add_argument("--test-env", default="RTSTest_SC36")
    namerts_parser.add_argument("--ground-truth", default=DEFAULT_OUTPUT)
    namerts_parser.add_argument("--run-root", default=DEFAULT_RUN_ROOT)
    namerts_parser.add_argument("--workers", type=int, default=40)
    namerts_parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help="Leave the applied patch and NameRTS conftest changes for debugging",
    )
    namerts_parser.add_argument(
        "--reuse-parent-cache",
        action="store_true",
        help="Reuse an existing NameRTS parent cache instead of rebuilding it",
    )
    namerts_parser.set_defaults(handler=namerts)

    batch_parser = subparsers.add_parser(
        "batch",
        help="Rebuild extensions and run ground truth plus NameRTS for multiple instances",
    )
    batch_parser.add_argument("--instances", nargs="*")
    batch_parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    batch_parser.add_argument("--repo", default=DEFAULT_REPO)
    batch_parser.add_argument("--test-env", default="RTSTest_SC36")
    batch_parser.add_argument("--ground-truth", default=DEFAULT_OUTPUT)
    batch_parser.add_argument("--run-root", default=DEFAULT_RUN_ROOT)
    batch_parser.add_argument("--status", default=DEFAULT_BATCH_STATUS)
    batch_parser.add_argument("--ground-truth-workers", type=int, default=40)
    batch_parser.add_argument("--namerts-workers", type=int, default=40)
    batch_parser.add_argument("--build-workers", type=int, default=8)
    batch_parser.add_argument("--timeout", type=int, default=900)
    batch_parser.add_argument("--force-ground-truth", action="store_true")
    batch_parser.add_argument("--force-namerts", action="store_true")
    batch_parser.add_argument("--ground-truth-only", action="store_true")
    batch_parser.add_argument("--namerts-only", action="store_true")
    batch_parser.add_argument("--reuse-parent-cache", action="store_true")
    batch_parser.add_argument("--continue-on-error", action="store_true")
    batch_parser.set_defaults(handler=batch)

    trace_parser = subparsers.add_parser(
        "trace",
        help="Trace patched-function calls to pytest node ids for selected test files",
    )
    trace_parser.add_argument("--instance", required=True)
    trace_parser.add_argument("--test-files", nargs="+", required=True)
    trace_parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    trace_parser.add_argument("--repo", default=DEFAULT_REPO)
    trace_parser.add_argument("--test-env", default="RTSTest_SC36")
    trace_parser.add_argument("--run-root", default=DEFAULT_RUN_ROOT)
    trace_parser.add_argument("--workers", type=int, default=1)
    trace_parser.add_argument("--build-workers", type=int, default=8)
    trace_parser.add_argument("--timeout", type=int, default=900)
    trace_parser.add_argument("--keep-worktree", action="store_true")
    trace_parser.set_defaults(handler=trace_instance)

    review_parser = subparsers.add_parser(
        "apply-reviews",
        help="Apply semantic review decisions to dynamic ground-truth candidates",
    )
    review_parser.add_argument("--ground-truth", default=DEFAULT_OUTPUT)
    review_parser.add_argument("--reviews", default=DEFAULT_SEMANTIC_REVIEWS)
    review_parser.set_defaults(handler=apply_semantic_reviews)

    summary_parser = subparsers.add_parser(
        "summarize",
        help="Summarize reviewed ground truth and the latest NameRTS comparisons",
    )
    summary_parser.add_argument("--ground-truth", default=DEFAULT_OUTPUT)
    summary_parser.add_argument("--run-root", default=DEFAULT_RUN_ROOT)
    summary_parser.add_argument("--output", default=DEFAULT_FINAL_SUMMARY)
    summary_parser.set_defaults(handler=summarize_results)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
