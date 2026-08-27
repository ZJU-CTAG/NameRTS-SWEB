"""Django validation for NameRTS without pytest or conftest hooks.

The orchestration code is compatible with Python 3.6 and newer.  Django tests
are executed one module per process through ``tests/runtests.py``.
"""

import argparse
import csv
import hashlib
import json
import os
import signal
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.config import get_test_target
from src.utils import collect_all_heuristic


DATA_ROOT = Path("/shared_dir/django_python36_python39_golden_patches")
DEFAULT_MANIFEST = DATA_ROOT / "manifest.tsv"
DEFAULT_SUBSET = DATA_ROOT / "selected_subset.json"
DEFAULT_REPO = (
    Path(__file__).resolve().parent.parent
    / "runtime"
    / "django_no_pytest"
    / "django"
)
DEFAULT_RUN_ROOT = (
    Path(__file__).resolve().parent.parent / "runtime" / "django_no_pytest"
)
DEFAULT_GROUND_TRUTH = (
    Path(__file__).resolve().parent.parent
    / "ground truth"
    / "gt_django_no_pytest.json"
)
DEFAULT_SEMANTIC_REVIEWS = (
    Path(__file__).resolve().parent.parent
    / "ground truth"
    / "django_no_pytest_semantic_reviews.json"
)
DEFAULT_ENV_ROOT = (
    Path(__file__).resolve().parent.parent
    / "environments"
    / "django_no_pytest"
)
DEFAULT_ENV_MANIFEST = DEFAULT_ENV_ROOT / "environment_manifest.json"
DEFAULT_FINAL_SUMMARY = DEFAULT_RUN_ROOT / "final_summary.json"
RESOURCE_ROOT = Path(
    "/shared_dir/swe-bench-repos/SWE-bench/"
    "swebench/resources/swebench-og/django__django"
)
SELECTION_SEED = "namerts-django-no-pytest-version-v1"
HIT_FILE_ENV = "NAMERTS_GT_HIT_FILE"
DETAIL_FILE_ENV = "NAMERTS_GT_DETAIL_FILE"


def _run(command, cwd=None, env=None, check=True, timeout=None):
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            "Command failed ({}): {}\nstdout:\n{}\nstderr:\n{}".format(
                result.returncode,
                " ".join(str(item) for item in command),
                result.stdout,
                result.stderr,
            )
        )
    return result


def _write_json(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")


def _load_json_records(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as input_file:
        value = json.load(input_file)
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array: {}".format(path))
    return value


def _update_json_record(path, record):
    records = _load_json_records(path)
    records = [
        item
        for item in records
        if item.get("instance_id") != record["instance_id"]
    ]
    records.append(record)
    records.sort(key=lambda item: item["instance_id"])
    _write_json(path, records)


def load_manifest(path):
    manifest_path = Path(path).resolve()
    records = []
    with manifest_path.open("r", encoding="utf-8", newline="") as manifest_file:
        for row in csv.DictReader(manifest_file, delimiter="\t"):
            record = dict(row)
            record["patch_path"] = str(
                (manifest_path.parent / record["patch_file"]).resolve()
            )
            records.append(record)
    return records


def load_subset(path):
    subset_path = Path(path).resolve()
    with subset_path.open("r", encoding="utf-8") as subset_file:
        document = json.load(subset_file)
    records = []
    for source in document["instances"]:
        record = dict(source)
        record["patch_path"] = str(
            (subset_path.parent / record["patch_file"]).resolve()
        )
        records.append(record)
    return records


def select_record(records, selector):
    matches = [
        record
        for record in records
        if selector in (record["instance_id"], record["base_commit"])
    ]
    if len(matches) != 1:
        raise ValueError(
            "Expected one record for {!r}, found {}".format(
                selector, len(matches)
            )
        )
    return matches[0]


def selection_hash(instance_id, seed=SELECTION_SEED):
    return hashlib.sha256(
        (seed + "\0" + instance_id).encode("utf-8")
    ).hexdigest()


def command_select_subset(args):
    records = load_manifest(args.manifest)
    groups = {}
    for record in records:
        key = (record["python_version"], record["project_version"])
        groups.setdefault(key, []).append(record)
    selected = []
    for key in sorted(groups):
        record = min(
            groups[key],
            key=lambda item: selection_hash(item["instance_id"], args.seed),
        )
        output_record = {
            key: value
            for key, value in record.items()
            if key != "patch_path"
        }
        output_record["selection_hash"] = selection_hash(
            record["instance_id"], args.seed
        )
        selected.append(output_record)
    document = {
        "schema_version": 1,
        "seed": args.seed,
        "strategy": (
            "minimum SHA-256(seed + NUL + instance_id) per Python x "
            "Django version"
        ),
        "source_manifest": str(Path(args.manifest).resolve()),
        "total_manifest_instances": len(records),
        "selected_count": len(selected),
        "instances": selected,
    }
    _write_json(args.output, document)
    print(
        "Selected {} of {} instances -> {}".format(
            len(selected), len(records), Path(args.output).resolve()
        )
    )


def _normalize_environment(source, environment_name):
    result = []
    replaced = False
    for line in source.splitlines():
        if line.startswith("name:"):
            result.append("name: {}".format(environment_name))
            replaced = True
        elif line.startswith("prefix:"):
            continue
        else:
            result.append(line)
    if not replaced:
        raise ValueError("SWE-bench environment has no name")
    return "\n".join(result).rstrip() + "\n"


def command_generate_envs(args):
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    definitions = [
        {
            "environment_name": "NameRTSDjango36",
            "python_version": "3.6",
            "django_versions": ["3.0", "3.1", "3.2"],
            # Django 3.2 has the largest Python 3.6 dependency superset.
            "source_issue": "13516",
        },
        {
            "environment_name": "NameRTSDjango39",
            "python_version": "3.9",
            "django_versions": ["4.1", "4.2"],
            "source_issue": "15368",
        },
    ]
    version_environments = {}
    for definition in definitions:
        source_path = (
            Path(args.resource_root).resolve()
            / definition["source_issue"]
            / "environment.yml"
        )
        output_path = output_root / (
            "environment_django_{}.yml".format(
                definition["python_version"].replace(".", "")
            )
        )
        output_path.write_text(
            _normalize_environment(
                source_path.read_text(encoding="utf-8"),
                definition["environment_name"],
            ),
            encoding="utf-8",
        )
        definition["source"] = str(source_path)
        definition["file"] = str(output_path)
        definition["pytest_required"] = False
        for version in definition["django_versions"]:
            version_environments[version] = definition["environment_name"]
    manifest = {
        "schema_version": 1,
        "environments": definitions,
        "project_version_environments": version_environments,
        "namerts_environments": {
            "3.6": "NameRTS36",
            "3.9": "NameRTS39",
        },
        "notes": [
            "Target environments intentionally do not install pytest.",
            "Django is installed editable from the checked-out validation worktree.",
        ],
    }
    _write_json(output_root / "environment_manifest.json", manifest)
    print("Generated 2 target yml files -> {}".format(output_root))


def load_environment_manifest(path):
    with Path(path).resolve().open("r", encoding="utf-8") as input_file:
        return json.load(input_file)


def target_environment(record, environment_manifest):
    return environment_manifest["project_version_environments"][
        record["project_version"]
    ]


def namerts_environment(record, environment_manifest):
    return environment_manifest["namerts_environments"][
        record["python_version"]
    ]


def resolve_conda_python(environment_name):
    result = _run(
        [
            "conda",
            "run",
            "-n",
            environment_name,
            "python",
            "-c",
            "import sys; print(sys.executable)",
        ]
    )
    candidates = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    if not candidates or not Path(candidates[-1]).exists():
        raise RuntimeError(
            "Cannot resolve Python for {}".format(environment_name)
        )
    return candidates[-1]


def assert_clean_tracked_worktree(repo):
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo
    ).stdout.strip()
    if status:
        raise RuntimeError(
            "Refusing to reset a worktree with tracked changes:\n{}".format(
                status
            )
        )


def cleanup_runtime_artifacts(repo):
    repo = Path(repo).resolve()
    coverage_dir = repo / "coverage"
    if coverage_dir.is_dir():
        shutil.rmtree(str(coverage_dir))
    for name in (
        "tests_to_run.txt",
        "dependencies.json",
        "py_checksums_cache.json",
        "nbdp_cache.json",
        "critical_names.json",
    ):
        path = repo / name
        if path.is_file():
            path.unlink()


def reset_to_base(repo, base_commit):
    _run(["git", "reset", "--hard", base_commit], cwd=repo)
    cleanup_runtime_artifacts(repo)


def install_django(repo, environment_name, log_path):
    test_python = resolve_conda_python(environment_name)
    command = [
        test_python,
        "-m",
        "pip",
        "install",
        "-e",
        ".",
        "--no-deps",
        "--no-build-isolation",
    ]
    started = time.time()
    result = _run(command, cwd=repo, check=False)
    log_path = Path(log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "command={}\nreturn_code={}\nelapsed_seconds={:.3f}\n\n"
        "stdout:\n{}\n\nstderr:\n{}".format(
            " ".join(command),
            result.returncode,
            time.time() - started,
            result.stdout,
            result.stderr,
        ),
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError("Django install failed; see {}".format(log_path))
    import_result = _run(
        [
            test_python,
            "-c",
            "import os,django; print(os.path.realpath(django.__file__))",
        ],
        cwd=repo,
    )
    imported_path = import_result.stdout.strip().splitlines()[-1]
    expected_root = str((Path(repo).resolve() / "django"))
    if not (
        imported_path == expected_root
        or imported_path.startswith(expected_root + os.sep)
    ):
        raise RuntimeError(
            "Editable install points outside validation worktree: {}".format(
                imported_path
            )
        )
    return test_python


def safe_test_id(test_file):
    digest = hashlib.sha1(test_file.encode("utf-8")).hexdigest()[:12]
    return "{}_{}".format(digest, Path(test_file).name)


def django_test_command(test_python, repo, test_file, verbosity="1"):
    return [
        test_python,
        str(Path(repo).resolve() / "tests" / "runtests.py"),
        "--verbosity",
        str(verbosity),
        "--settings=test_sqlite",
        "--parallel",
        "1",
        get_test_target(str(repo), test_file),
    ]


def run_one_test(
    test_python, repo, test_file, run_dir, timeout, collect_hits=True
):
    test_id = safe_test_id(test_file)
    hit_path = run_dir / "hits" / (test_id + ".txt")
    detail_path = run_dir / "details" / (test_id + ".txt")
    log_path = run_dir / "logs" / (test_id + ".log")
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment["OMP_NUM_THREADS"] = "1"
    environment["OPENBLAS_NUM_THREADS"] = "1"
    environment["MKL_NUM_THREADS"] = "1"
    if collect_hits:
        environment[HIT_FILE_ENV] = str(hit_path)
        environment[DETAIL_FILE_ENV] = str(detail_path)
    command = django_test_command(test_python, repo, test_file)
    started = time.time()
    process = subprocess.Popen(
        command,
        cwd=str(repo),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        universal_newlines=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return_code = process.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        return_code = -1
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        killed_stdout, killed_stderr = process.communicate()
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        if killed_stdout and killed_stdout not in stdout:
            stdout += killed_stdout
        if killed_stderr and killed_stderr not in stderr:
            stderr += killed_stderr
    log_path.write_text(
        "{}command={}\nreturn_code={}\n\nstdout:\n{}\n\nstderr:\n{}".format(
            "TIMEOUT after {} seconds\n".format(timeout) if timed_out else "",
            " ".join(command),
            return_code,
            stdout,
            stderr,
        ),
        encoding="utf-8",
    )
    markers = (
        sorted(set(hit_path.read_text(encoding="utf-8").splitlines()))
        if hit_path.exists()
        else []
    )
    details = (
        sorted(set(detail_path.read_text(encoding="utf-8").splitlines()))
        if detail_path.exists()
        else []
    )
    return {
        "test_file": test_file,
        "test_label": get_test_target(str(repo), test_file),
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
    pool_size = min(max(1, workers), total or 1)
    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        futures = {
            executor.submit(
                run_one_test,
                test_python,
                repo,
                test_file,
                run_dir,
                timeout,
            ): test_file
            for test_file in test_files
        }
        for index, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if index == total or index % 20 == 0:
                print(
                    "Django test-file progress: {}/{}".format(index, total),
                    flush=True,
                )
    return sorted(results, key=lambda item: item["test_file"])


def validation_context(args):
    records = load_subset(args.subset)
    record = select_record(records, args.instance)
    environments = load_environment_manifest(args.environment_manifest)
    return record, environments


def command_smoke(args):
    record, environments = validation_context(args)
    repo = Path(args.repo).resolve()
    environment_name = target_environment(record, environments)
    assert_clean_tracked_worktree(repo)
    try:
        reset_to_base(repo, record["base_commit"])
        test_python = install_django(
            repo,
            environment_name,
            Path(args.run_root)
            / "installs"
            / ("smoke_" + record["instance_id"] + ".log"),
        )
        test_files = sorted(collect_all_heuristic(str(repo)))
        preferred = "tests/utils_tests/test_module_loading.py"
        test_file = preferred if preferred in test_files else test_files[0]
        run_dir = (
            Path(args.run_root).resolve()
            / "smoke"
            / record["instance_id"]
        )
        for name in ("hits", "details", "logs"):
            (run_dir / name).mkdir(parents=True, exist_ok=True)
        result = run_one_test(
            test_python,
            repo,
            test_file,
            run_dir,
            args.timeout,
            collect_hits=False,
        )
        result.update(
            {
                "instance_id": record["instance_id"],
                "base_commit": record["base_commit"],
                "project_version": record["project_version"],
                "python_version": record["python_version"],
                "test_env": environment_name,
                "total_test_files": len(test_files),
                "pytest_installed": (
                    _run(
                        [
                            test_python,
                            "-c",
                            "import importlib.util; "
                            "print(bool(importlib.util.find_spec('pytest')))",
                        ]
                    ).stdout.strip()
                    == "True"
                ),
                "safe": result["return_code"] == 0,
            }
        )
        _write_json(run_dir / "result.json", result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["safe"] else 1
    finally:
        reset_to_base(repo, record["base_commit"])


def command_ground_truth(args):
    from src import sklearn_python36_validation as helpers

    record, environments = validation_context(args)
    repo = Path(args.repo).resolve()
    environment_name = target_environment(record, environments)
    patch_path = Path(record["patch_path"]).resolve()
    patch_text = patch_path.read_text(encoding="utf-8")
    changed_lines = helpers.parse_patch_changed_lines(patch_text)
    assert_clean_tracked_worktree(repo)
    try:
        reset_to_base(repo, record["base_commit"])
        test_python = install_django(
            repo,
            environment_name,
            Path(args.run_root)
            / "installs"
            / (record["instance_id"] + ".log"),
        )
        _run(["git", "apply", "--check", str(patch_path)], cwd=repo)
        _run(["git", "apply", str(patch_path)], cwd=repo)
        functions, uninstrumentable = helpers.identify_modified_functions(
            repo, record["base_commit"], patch_text, changed_lines
        )
        if not functions:
            raise RuntimeError(
                "No instrumentable Python function in {}".format(patch_path)
            )
        helpers.instrument_functions(repo, functions)
        run_id = "{}_{}_{}".format(
            record["instance_id"], int(time.time()), os.getpid()
        )
        run_dir = (
            Path(args.run_root).resolve() / "ground_truth_runs" / run_id
        )
        for name in ("hits", "details", "logs"):
            (run_dir / name).mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "instrumentation.json",
            {
                "functions": functions,
                "uninstrumentable": uninstrumentable,
            },
        )
        test_files = sorted(collect_all_heuristic(str(repo)))
        print(
            "Running {} Django test files for {} with {} workers".format(
                len(test_files), record["instance_id"], args.workers
            ),
            flush=True,
        )
        started = time.time()
        test_results = run_tests(
            test_python,
            repo,
            test_files,
            run_dir,
            args.workers,
            args.timeout,
        )
        dynamic_hits_all = sorted(
            item["test_file"] for item in test_results if item["markers"]
        )
        passing_dynamic_hits = sorted(
            item["test_file"]
            for item in test_results
            if item["return_code"] == 0 and item["markers"]
        )
        failures = sorted(
            item["test_file"]
            for item in test_results
            if item["return_code"] != 0 and not item["timed_out"]
        )
        timeouts = sorted(
            item["test_file"] for item in test_results if item["timed_out"]
        )
        result = {
            "instance_id": record["instance_id"],
            "base_commit": record["base_commit"],
            "patch_file": record["patch_file"],
            "project_version": record["project_version"],
            "python_version": record["python_version"],
            "test_env": environment_name,
            "modified_functions": functions,
            "uninstrumentable_changes": uninstrumentable,
            "dynamic_hit_tests_all": dynamic_hits_all,
            # A patch can make an old test fail precisely because that test is
            # affected (for example, changing the subprocess API it mocks).
            # Keep every entry hit as the candidate ground truth and preserve
            # the passing-only view as diagnostic metadata.
            "dynamic_hit_tests": dynamic_hits_all,
            "passing_dynamic_hit_tests": passing_dynamic_hits,
            "tests_to_run": dynamic_hits_all,
            "affected_functions_by_test": {
                item["test_file"]: item["markers"]
                for item in test_results
                if item["markers"]
            },
            "total_test_files": len(test_files),
            "passed_test_files": (
                len(test_files) - len(failures) - len(timeouts)
            ),
            "failed_test_files": failures,
            "timed_out_test_files": timeouts,
            "elapsed_seconds": round(time.time() - started, 3),
            "run_dir": str(run_dir),
        }
        _update_json_record(args.ground_truth, result)
        _write_json(run_dir / "test_results.json", test_results)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        reset_to_base(repo, record["base_commit"])


def command_namerts(args):
    from src.evaluate import (
        CACHED_FILES_NBDP,
        evaluate_instance,
        install_instrumentor,
    )
    from src.namebdp import NameBDP
    import src.config as namerts_config

    record, environments = validation_context(args)
    repo = Path(args.repo).resolve()
    environment_name = target_environment(record, environments)
    ground_truth = [
        item
        for item in _load_json_records(args.ground_truth)
        if item.get("instance_id") == record["instance_id"]
    ]
    if len(ground_truth) != 1:
        raise RuntimeError(
            "Expected one ground-truth record for {}, found {}".format(
                record["instance_id"], len(ground_truth)
            )
        )
    assert_clean_tracked_worktree(repo)
    namerts_config.PER_FILE_TEST_TIMEOUT = args.timeout
    try:
        reset_to_base(repo, record["base_commit"])
        install_django(
            repo,
            environment_name,
            Path(args.run_root)
            / "installs"
            / ("namerts_" + record["instance_id"] + ".log"),
        )
        install_instrumentor(environment_name)
        result = evaluate_instance(
            direct_parent=record["base_commit"],
            true_parent=record["base_commit"],
            current=None,
            target=True,
            tool_name="NameBDP",
            repo_path=str(repo),
            conda_env=environment_name,
            tool_class=NameBDP,
            cached_files=CACHED_FILES_NBDP,
            time_tag="django_no_pytest_{}_{}".format(
                record["instance_id"], time.time()
            ),
            n=args.workers,
            registry_decorator_keywords=set(),
            use_isolation=False,
            run_parent=True,
            patch_path=record["patch_path"],
            run_current_tests=False,
            reuse_parent_cache=args.reuse_parent_cache,
        )
        selected = sorted(
            set(Path(path).as_posix() for path in result["tests_to_run"])
        )
        expected = sorted(
            set(
                Path(path).as_posix()
                for path in ground_truth[0]["tests_to_run"]
            )
        )
        missing = sorted(set(expected) - set(selected))
        comparison = {
            "instance_id": record["instance_id"],
            "base_commit": record["base_commit"],
            "patch_file": record["patch_file"],
            "project_version": record["project_version"],
            "python_version": record["python_version"],
            "test_env": environment_name,
            "ground_truth": expected,
            "tests_to_run": selected,
            "missing_tests": missing,
            "safe": not missing,
            "namerts_result": result,
        }
        output = (
            Path(args.run_root).resolve()
            / "comparisons"
            / ("namerts_{}_{}.json".format(
                record["instance_id"], int(time.time())
            ))
        )
        _write_json(output, comparison)
        comparison["output"] = str(output)
        print(json.dumps(comparison, indent=2, sort_keys=True))
        return 0 if comparison["safe"] else 1
    finally:
        reset_to_base(repo, record["base_commit"])


def command_apply_semantic_reviews(args):
    reviews = {
        item["instance_id"]: item
        for item in _load_json_records(args.reviews)
    }
    records = _load_json_records(args.ground_truth)
    applied = 0
    for record in records:
        review = reviews.get(record["instance_id"])
        if review is None:
            continue
        dynamic_hits = set(record.get("dynamic_hit_tests", []))
        if review.get("exclude_all_dynamic_hits"):
            excluded = set(dynamic_hits)
        else:
            excluded = dynamic_hits & set(
                review.get("exclude_tests", [])
            )
        included = dynamic_hits & set(review.get("include_tests", []))
        record["semantic_excluded_tests"] = sorted(excluded)
        record["tests_to_run"] = sorted(
            (dynamic_hits - excluded) | included
        )
        record["semantic_review"] = {
            key: value
            for key, value in review.items()
            if key not in (
                "exclude_all_dynamic_hits",
                "exclude_tests",
                "include_tests",
            )
        }
        applied += 1
    _write_json(args.ground_truth, records)
    print("Applied {} Django semantic reviews".format(applied))
    return 0


def command_batch(args):
    records = load_subset(args.subset)
    status_path = Path(args.run_root).resolve() / "batch_status.json"
    statuses = {}
    if status_path.exists():
        with status_path.open("r", encoding="utf-8") as status_file:
            statuses = json.load(status_file)
    overall = 0
    for phase, command in (
        ("ground_truth", command_ground_truth),
        ("namerts", command_namerts),
    ):
        if phase == "namerts":
            review_args = argparse.Namespace(
                ground_truth=args.ground_truth,
                reviews=args.reviews,
            )
            command_apply_semantic_reviews(review_args)
        for index, record in enumerate(records, start=1):
            instance = record["instance_id"]
            print(
                "Django {} {}/{}: {}".format(
                    phase, index, len(records), instance
                ),
                flush=True,
            )
            instance_status = statuses.setdefault(instance, {})
            if getattr(args, "skip_completed", False) and instance_status.get(
                phase
            ) == "complete":
                continue
            phase_args = argparse.Namespace(**vars(args))
            phase_args.instance = instance
            try:
                return_code = command(phase_args)
                instance_status[phase] = (
                    "complete" if return_code == 0 else "comparison_missing"
                )
                if return_code != 0:
                    overall = 1
            except Exception as error:
                instance_status[phase] = "error: {}".format(error)
                overall = 1
                if not args.continue_on_error:
                    _write_json(status_path, statuses)
                    raise
            _write_json(status_path, statuses)
    return overall


def command_summarize(args):
    records = _load_json_records(args.ground_truth)
    comparison_root = Path(args.run_root).resolve() / "comparisons"
    instances = []
    for record in records:
        paths = list(
            comparison_root.glob(
                "namerts_{}_*.json".format(record["instance_id"])
            )
        )
        if not paths:
            raise RuntimeError(
                "No comparison for {}".format(record["instance_id"])
            )
        comparison_path = max(
            paths, key=lambda path: (path.stat().st_mtime, path.name)
        )
        with comparison_path.open(
            "r", encoding="utf-8"
        ) as comparison_file:
            comparison = json.load(comparison_file)
        final_ground_truth = sorted(set(record.get("tests_to_run", [])))
        selected = sorted(set(comparison.get("tests_to_run", [])))
        missing = sorted(set(final_ground_truth) - set(selected))
        instances.append(
            {
                "instance_id": record["instance_id"],
                "base_commit": record["base_commit"],
                "python_version": record["python_version"],
                "project_version": record["project_version"],
                "total_test_files": record["total_test_files"],
                "passed_test_files": record["passed_test_files"],
                "failed_test_files": len(
                    record.get("failed_test_files", [])
                ),
                "timed_out_test_files": len(
                    record.get("timed_out_test_files", [])
                ),
                "dynamic_hit_tests": sorted(
                    record.get("dynamic_hit_tests", [])
                ),
                "semantic_excluded_tests": sorted(
                    record.get("semantic_excluded_tests", [])
                ),
                "ground_truth": final_ground_truth,
                "tests_to_run": selected,
                "missing_tests": missing,
                "safe": not missing,
                "ground_truth_elapsed_seconds": record[
                    "elapsed_seconds"
                ],
                "namerts_init_time": comparison["namerts_result"][
                    "init_time"
                ],
                "namerts_select_time": comparison["namerts_result"][
                    "select_time"
                ],
                "comparison": str(comparison_path),
            }
        )
    aggregate = {
        "instances": len(instances),
        "safe_instances": sum(1 for item in instances if item["safe"]),
        "total_test_file_runs": sum(
            item["total_test_files"] for item in instances
        ),
        "passed_test_file_runs": sum(
            item["passed_test_files"] for item in instances
        ),
        "failed_test_file_runs": sum(
            item["failed_test_files"] for item in instances
        ),
        "timed_out_test_file_runs": sum(
            item["timed_out_test_files"] for item in instances
        ),
        "dynamic_hit_test_files": sum(
            len(item["dynamic_hit_tests"]) for item in instances
        ),
        "semantic_excluded_test_files": sum(
            len(item["semantic_excluded_tests"]) for item in instances
        ),
        "final_ground_truth_test_files": sum(
            len(item["ground_truth"]) for item in instances
        ),
        "selected_test_files": sum(
            len(item["tests_to_run"]) for item in instances
        ),
        "missing_test_files": sum(
            len(item["missing_tests"]) for item in instances
        ),
    }
    summary = {
        "schema_version": 1,
        "aggregate": aggregate,
        "instances": sorted(
            instances, key=lambda item: item["instance_id"]
        ),
    }
    _write_json(args.output, summary)
    print(json.dumps(aggregate, indent=2, sort_keys=True))
    return 0


def build_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    select_parser = subparsers.add_parser("select-subset")
    select_parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    select_parser.add_argument("--output", default=str(DEFAULT_SUBSET))
    select_parser.add_argument("--seed", default=SELECTION_SEED)
    select_parser.set_defaults(func=command_select_subset)

    env_parser = subparsers.add_parser("generate-envs")
    env_parser.add_argument("--resource-root", default=str(RESOURCE_ROOT))
    env_parser.add_argument("--output-root", default=str(DEFAULT_ENV_ROOT))
    env_parser.set_defaults(func=command_generate_envs)

    review_parser = subparsers.add_parser("apply-semantic-reviews")
    review_parser.add_argument(
        "--ground-truth", default=str(DEFAULT_GROUND_TRUTH)
    )
    review_parser.add_argument(
        "--reviews", default=str(DEFAULT_SEMANTIC_REVIEWS)
    )
    review_parser.set_defaults(func=command_apply_semantic_reviews)

    for name, function in (
        ("smoke", command_smoke),
        ("ground-truth", command_ground_truth),
        ("namerts", command_namerts),
        ("batch", command_batch),
    ):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--instance", default=None)
        command_parser.add_argument("--subset", default=str(DEFAULT_SUBSET))
        command_parser.add_argument("--repo", default=str(DEFAULT_REPO))
        command_parser.add_argument(
            "--environment-manifest", default=str(DEFAULT_ENV_MANIFEST)
        )
        command_parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
        command_parser.add_argument(
            "--ground-truth", "--output",
            dest="ground_truth",
            default=str(DEFAULT_GROUND_TRUTH),
        )
        command_parser.add_argument("--workers", type=int, default=24)
        command_parser.add_argument("--timeout", type=int, default=300)
        command_parser.add_argument(
            "--reuse-parent-cache", action="store_true"
        )
        command_parser.add_argument(
            "--continue-on-error", action="store_true"
        )
        command_parser.add_argument(
            "--skip-completed", action="store_true"
        )
        command_parser.add_argument(
            "--reviews", default=str(DEFAULT_SEMANTIC_REVIEWS)
        )
        command_parser.set_defaults(func=function)

    summary_parser = subparsers.add_parser("summarize")
    summary_parser.add_argument(
        "--ground-truth", default=str(DEFAULT_GROUND_TRUTH)
    )
    summary_parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    summary_parser.add_argument("--output", default=str(DEFAULT_FINAL_SUMMARY))
    summary_parser.set_defaults(func=command_summarize)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    if args.command in ("smoke", "ground-truth", "namerts") and not args.instance:
        parser.error("{} requires --instance".format(args.command))
    return args.func(args)


if __name__ == "__main__":
    sys_exit = main()
    raise SystemExit(sys_exit)
