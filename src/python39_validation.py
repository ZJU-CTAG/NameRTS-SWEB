"""Python 3.9 compatibility validation for four SWE-bench projects.

The commands in this module are deliberately compatible with Python 3.6 and
newer so that the same orchestration code can be regression-tested in every
NameRTS environment.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


DEFAULT_DATA_ROOT = Path(
    "/shared_dir/python39_sympy_sphinx_astropy_pylint_golden_patches"
)
DEFAULT_MANIFEST = DEFAULT_DATA_ROOT / "manifest.tsv"
DEFAULT_SUBSET = DEFAULT_DATA_ROOT / "selected_subset.json"
DEFAULT_VALIDATED_SUBSET = DEFAULT_DATA_ROOT / "validated_subset.json"
DEFAULT_RESOURCE_ROOT = Path(
    "/shared_dir/swe-bench-repos/SWE-bench/swebench/resources/swebench-og"
)
DEFAULT_ENV_ROOT = Path(__file__).resolve().parent.parent / "environments" / "python39_targets"
DEFAULT_ENV_MANIFEST = DEFAULT_ENV_ROOT / "environment_manifest.json"
DEFAULT_REPOS_ROOT = Path("/shared_dir/swe-bench-repos")
DEFAULT_RUN_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "python39"
DEFAULT_GROUND_TRUTH = (
    Path(__file__).resolve().parent.parent
    / "ground truth"
    / "gt_python39_selected.json"
)
DEFAULT_BATCH_STATUS = DEFAULT_RUN_ROOT / "batch_status.json"
DEFAULT_FINAL_SUMMARY = DEFAULT_RUN_ROOT / "final_summary.json"
SELECTION_SEED = "namerts-python39-project-version-v1"

PROJECTS = {
    "astropy/astropy": {
        "slug": "astropy",
        "repo": str(DEFAULT_REPOS_ROOT / "astropy"),
        "registry_decorators": [],
    },
    "pylint-dev/pylint": {
        "slug": "pylint",
        "repo": str(DEFAULT_REPOS_ROOT / "pylint"),
        "registry_decorators": [],
    },
    "sphinx-doc/sphinx": {
        "slug": "sphinx",
        "repo": str(DEFAULT_REPOS_ROOT / "sphinx"),
        "registry_decorators": [],
    },
    "sympy/sympy": {
        "slug": "sympy",
        "repo": str(DEFAULT_REPOS_ROOT / "sympy"),
        "registry_decorators": ["register"],
    },
}

PATCH_DEPENDENCIES = {
    # The golden patch adds this requirement to setup.cfg.  The SWE-bench
    # parent environment predates the patch, so install it before running
    # patched ground-truth tests.
    "pylint-dev__pylint-4661": ["appdirs>=1.4.0"],
}


def load_manifest(path):
    manifest_path = Path(path).resolve()
    records = []
    with manifest_path.open("r", encoding="utf-8", newline="") as manifest_file:
        for row in csv.DictReader(manifest_file, delimiter="\t"):
            record = dict(row)
            record["patch_path"] = str(
                (manifest_path.parent / row["patch_file"]).resolve()
            )
            records.append(record)
    return records


def _selection_key(record, seed):
    value = "\0".join(
        [seed, record["repo"], record["version"], record["instance_id"]]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def select_project_version_subset(records, seed=SELECTION_SEED):
    """Select exactly one deterministic representative per repo/version pair."""
    groups = defaultdict(list)
    for record in records:
        groups[(record["repo"], record["version"])].append(record)

    selected = []
    for group in sorted(groups):
        selected.append(
            min(groups[group], key=lambda item: _selection_key(item, seed))
        )
    return selected


def build_subset_document(records, selected, manifest_path, seed):
    all_versions = defaultdict(set)
    selected_versions = defaultdict(set)
    for record in records:
        all_versions[record["repo"]].add(record["version"])
    for record in selected:
        selected_versions[record["repo"]].add(record["version"])

    return {
        "schema_version": 1,
        "source_manifest": str(Path(manifest_path).resolve()),
        "manifest_record_count": len(records),
        "selection_seed": seed,
        "selection_strategy": (
            "For each (repo, version), choose the record with the minimum "
            "SHA-256 of seed, repo, version, and instance_id."
        ),
        "selected_count": len(selected),
        "all_project_counts": dict(
            sorted(Counter(item["repo"] for item in records).items())
        ),
        "selected_project_counts": dict(
            sorted(Counter(item["repo"] for item in selected).items())
        ),
        "version_coverage": {
            repo: {
                "available": sorted(
                    all_versions[repo], key=lambda value: tuple(
                        int(part) for part in value.split(".")
                    )
                ),
                "selected": sorted(
                    selected_versions[repo], key=lambda value: tuple(
                        int(part) for part in value.split(".")
                    )
                ),
            }
            for repo in sorted(all_versions)
        },
        "instances": selected,
    }


def command_select_subset(args):
    records = load_manifest(args.manifest)
    selected = select_project_version_subset(records, seed=args.seed)
    document = build_subset_document(records, selected, args.manifest, args.seed)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "Selected {} of {} records across {} project/version groups -> {}".format(
            len(selected), len(records), len(selected), output_path
        )
    )


def command_filter_subset(args):
    """Materialize the exact execution subset from the version-stratified set."""
    with Path(args.subset).resolve().open(
        "r", encoding="utf-8"
    ) as subset_file:
        source = json.load(subset_file)
    filters = {}
    for value in args.project_versions:
        if "=" not in value:
            raise ValueError(
                "Expected PROJECT=VERSION[,VERSION], got {!r}".format(value)
            )
        project, versions = value.split("=", 1)
        if project not in PROJECTS:
            raise ValueError("Unknown project: {}".format(project))
        filters[project] = set(
            version for version in versions.split(",") if version
        )
    instances = [
        record
        for record in source["instances"]
        if record["repo"] not in filters
        or record["version"] in filters[record["repo"]]
    ]
    document = dict(source)
    document["instances"] = instances
    document["selected_count"] = len(instances)
    document["selected_project_counts"] = dict(
        sorted(Counter(item["repo"] for item in instances).items())
    )
    selected_versions = defaultdict(list)
    for record in instances:
        selected_versions[record["repo"]].append(record["version"])
    for project, coverage in document["version_coverage"].items():
        coverage["selected"] = sorted(
            set(selected_versions.get(project, [])),
            key=lambda value: tuple(
                int(part) for part in value.split(".")
            ),
        )
    document["execution_filter"] = {
        project: sorted(
            versions,
            key=lambda value: tuple(
                int(part) for part in value.split(".")
            ),
        )
        for project, versions in sorted(filters.items())
    }
    document["selection_strategy"] = (
        source["selection_strategy"]
        + " The execution subset then applies the explicit project/version "
        "filter stored in execution_filter."
    )
    _write_json(args.output, document)
    print(
        "Selected {} execution records -> {}".format(
            len(instances), Path(args.output).resolve()
        )
    )


def load_subset(path):
    with Path(path).resolve().open("r", encoding="utf-8") as subset_file:
        document = json.load(subset_file)
    return document["instances"]


def select_record(records, selector):
    matches = [
        record
        for record in records
        if selector in (record["instance_id"], record["base_commit"])
    ]
    if len(matches) != 1:
        raise ValueError(
            "Expected one subset record for {!r}, found {}".format(
                selector, len(matches)
            )
        )
    return matches[0]


def load_environment_manifest(path):
    with Path(path).resolve().open("r", encoding="utf-8") as manifest_file:
        return json.load(manifest_file)


def project_configuration(record, environment_manifest):
    project = record["repo"]
    if project not in PROJECTS:
        raise ValueError("Unsupported project: {}".format(project))
    config = dict(PROJECTS[project])
    config["project"] = project
    config["test_env"] = environment_manifest["instance_environments"][
        record["instance_id"]
    ]
    return config


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
                process.returncode,
                " ".join(command),
                process.stdout,
                process.stderr,
            )
        )
    return process


def _write_json(path, value):
    output_path = Path(path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _load_json_list(path):
    input_path = Path(path).resolve()
    if not input_path.exists():
        return []
    value = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array: {}".format(input_path))
    return value


def update_json_records(path, record):
    records = [
        item
        for item in _load_json_list(path)
        if item.get("instance_id") != record["instance_id"]
    ]
    records.append(record)
    records.sort(key=lambda item: item["instance_id"])
    _write_json(path, records)


def shared_validation_helpers():
    """Import heavier NameRTS helpers only for execution commands."""
    from src import sklearn_python36_validation as helpers

    return helpers


def assert_clean_tracked_worktree(repo):
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo
    ).stdout.strip()
    if status:
        raise RuntimeError(
            "Refusing to reset a worktree with tracked changes:\n{}".format(status)
        )


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
    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not candidates or not Path(candidates[-1]).exists():
        raise RuntimeError(
            "Could not resolve Python for environment {}".format(environment_name)
        )
    return candidates[-1]


def interpreter_version(test_python):
    """Return the version even for Python releases that write it to stderr."""
    result = _run([test_python, "--version"], check=False)
    return (result.stdout.strip() or result.stderr.strip())


def clean_generated_extensions(repo, project):
    """Remove only untracked compiled artifacts that can cross commit boundaries."""
    repo = Path(repo).resolve()
    final_worktree_parts = (
        "runtime",
        "final_python_samples",
        "worktrees",
    )
    is_final_worktree = any(
        tuple(repo.parts[index : index + len(final_worktree_parts)])
        == final_worktree_parts
        for index in range(
            max(0, len(repo.parts) - len(final_worktree_parts) + 1)
        )
    )
    if project != "astropy/astropy" and not is_final_worktree:
        return 0
    tracked = set(
        _run(["git", "ls-files", "*.so"], cwd=repo).stdout.splitlines()
    )
    removed = 0
    extension_root = repo / "astropy" if project == "astropy/astropy" else repo
    for extension in extension_root.rglob("*.so"):
        relative = str(extension.relative_to(repo))
        if relative in tracked:
            raise RuntimeError(
                "Refusing to remove tracked extension: {}".format(relative)
            )
        extension.unlink()
        removed += 1
    if is_final_worktree:
        generated_directories = [repo / "build", repo / "dist"]
        generated_directories.extend(repo.glob("*.egg-info"))
        generated_directories.extend(repo.glob("*/*.egg-info"))
        for generated in generated_directories:
            if not generated.exists():
                continue
            relative = str(generated.relative_to(repo))
            tracked_contents = _run(
                ["git", "ls-files", "--", relative], cwd=repo
            ).stdout.splitlines()
            if tracked_contents:
                raise RuntimeError(
                    "Refusing to remove generated directory containing tracked "
                    "files: {}".format(relative)
                )
            if generated.is_dir():
                shutil.rmtree(str(generated))
            else:
                generated.unlink()
            removed += 1
    return removed


def install_project(record, config, log_path):
    """Install the checked-out source in its exact SWE-bench environment."""
    repo = Path(config["repo"]).resolve()
    test_python = resolve_conda_python(config["test_env"])
    removed_extensions = clean_generated_extensions(repo, record["repo"])
    metadata_adjustments = []
    if record["repo"] == "astropy/astropy":
        pyproject_path = repo / "pyproject.toml"
        if pyproject_path.exists():
            source = pyproject_path.read_text(encoding="utf-8")
            unpinned = 'requires = ["setuptools",'
            pinned = 'requires = ["setuptools==68.0.0",'
            if unpinned in source:
                pyproject_path.write_text(
                    source.replace(unpinned, pinned, 1), encoding="utf-8"
                )
                metadata_adjustments.append(
                    "Pinned Astropy build setuptools to 68.0.0 per SWE-bench pre_install"
                )
            elif pinned not in source:
                raise RuntimeError(
                    "Could not locate Astropy setuptools build requirement"
                )
    elif record["repo"] == "sphinx-doc/sphinx":
        major_minor = tuple(int(part) for part in record["version"].split(".")[:2])
        if major_minor <= (4, 3):
            setup_path = repo / "setup.py"
            source = setup_path.read_text(encoding="utf-8")
            replacements = [
                ("Jinja2>=2.3", "Jinja2<3.0"),
                (
                    "sphinxcontrib-applehelp",
                    "sphinxcontrib-applehelp<=1.0.7",
                ),
                (
                    "sphinxcontrib-devhelp",
                    "sphinxcontrib-devhelp<=1.0.5",
                ),
                (
                    "sphinxcontrib-qthelp",
                    "sphinxcontrib-qthelp<=1.0.6",
                ),
                (
                    "alabaster>=0.7,<0.8",
                    "alabaster>=0.7,<0.7.12",
                ),
                (
                    "'packaging',",
                    "'packaging', 'markupsafe<=2.0.1',",
                ),
            ]
            for old, new in replacements:
                if new not in source and old in source:
                    source = source.replace(old, new, 1)
                    metadata_adjustments.append(
                        "Sphinx dependency pin: {} -> {}".format(old, new)
                    )
            for dependency, maximum in (
                ("sphinxcontrib-htmlhelp", "2.0.4"),
                ("sphinxcontrib-serializinghtml", "1.1.9"),
            ):
                pattern = re.compile(
                    r"{}(?:>=\d+(?:\.\d+)*)?".format(re.escape(dependency))
                )
                match = pattern.search(source)
                replacement = "{}<={}".format(dependency, maximum)
                if match and replacement not in source:
                    source = (
                        source[: match.start()]
                        + replacement
                        + source[match.end() :]
                    )
                    metadata_adjustments.append(
                        "Sphinx dependency pin: {}".format(replacement)
                    )
            setup_path.write_text(source, encoding="utf-8")
    install_target = "."
    command = [
        test_python,
        "-m",
        "pip",
        "install",
        "-e",
        install_target,
    ]
    if record["repo"] != "sphinx-doc/sphinx":
        command.append("--no-deps")
    if record["repo"] not in (
        "astropy/astropy",
        "mwaskom/seaborn",
        "pytest-dev/pytest",
        "sphinx-doc/sphinx",
    ):
        command.append("--no-build-isolation")
    started = time.time()
    result = _run(command, cwd=repo, check=False)
    log_path = Path(log_path).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "command={}\nreturn_code={}\nelapsed_seconds={:.3f}\n"
        "removed_extensions={}\nmetadata_adjustments={}\n\n"
        "stdout:\n{}\n\nstderr:\n{}".format(
            " ".join(command),
            result.returncode,
            time.time() - started,
            removed_extensions,
            metadata_adjustments,
            result.stdout,
            result.stderr,
        ),
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Project install failed for {}; see {}".format(
                record["instance_id"], log_path
            )
        )
    return {
        "log": str(log_path),
        "removed_extensions": removed_extensions,
        "test_python": test_python,
    }


def collect_test_files(repo):
    from src.utils import collect_all_heuristic

    return sorted(collect_all_heuristic(str(repo)))


def _safe_test_id(test_file):
    digest = hashlib.sha1(test_file.encode("utf-8")).hexdigest()[:12]
    return "{}_{}".format(digest, Path(test_file).name)


def run_one_test(
    test_python, repo, project, test_file, run_dir, timeout, hit_env, detail_env
):
    safe_id = _safe_test_id(test_file)
    hit_path = run_dir / "hits" / (safe_id + ".txt")
    detail_path = run_dir / "details" / (safe_id + ".txt")
    log_path = run_dir / "logs" / (safe_id + ".log")
    environment = os.environ.copy()
    environment[hit_env] = str(hit_path)
    environment[detail_env] = str(detail_path)
    environment["PYTHONHASHSEED"] = "0"
    environment["OMP_NUM_THREADS"] = "1"
    environment["OPENBLAS_NUM_THREADS"] = "1"
    environment["MKL_NUM_THREADS"] = "1"
    if project == "django/django":
        normalized = test_file.replace("\\", "/")
        if normalized.startswith("tests/"):
            normalized = normalized[len("tests/"):]
        if normalized.endswith(".py"):
            normalized = normalized[:-len(".py")]
        test_target = normalized.replace("/", ".")
        command = [
            test_python,
            "./tests/runtests.py",
            "--verbosity",
            "1",
            "--settings=test_sqlite",
            "--parallel",
            "1",
            test_target,
        ]
    else:
        command = [
            test_python,
            "-m",
            "pytest",
            "-q",
            test_file,
            "-p",
            "no:cacheprovider",
        ]
    if project == "sphinx-doc/sphinx":
        command.extend(["-k", "not test_build_linkcheck"])
    started = time.time()
    timed_out = False
    process = subprocess.Popen(
        command,
        cwd=str(repo),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        universal_newlines=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return_code = process.returncode
        output = "stdout:\n{}\n\nstderr:\n{}".format(
            stdout, stderr
        )
    except subprocess.TimeoutExpired as error:
        timed_out = True
        return_code = -1
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout_after_kill, stderr_after_kill = process.communicate()
        stdout = (
            error.stdout.decode("utf-8", "replace")
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        stderr = (
            error.stderr.decode("utf-8", "replace")
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        if stdout_after_kill and stdout_after_kill not in stdout:
            stdout += stdout_after_kill
        if stderr_after_kill and stderr_after_kill not in stderr:
            stderr += stderr_after_kill
        output = (
            "TIMEOUT after {} seconds\nstdout:\n{}\n\nstderr:\n{}".format(
                timeout, stdout, stderr
            )
        )
    log_path.write_text(output, encoding="utf-8")
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
        "return_code": return_code,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 3),
        "markers": markers,
        "details": details,
        "log": str(log_path),
    }


def run_tests(
    test_python, repo, project, test_files, run_dir, workers, timeout, helpers
):
    results = []
    total = len(test_files)
    with ThreadPoolExecutor(max_workers=min(max(1, workers), total or 1)) as executor:
        futures = {
            executor.submit(
                run_one_test,
                test_python,
                repo,
                project,
                test_file,
                run_dir,
                timeout,
                helpers.HIT_FILE_ENV,
                helpers.DETAIL_FILE_ENV,
            ): test_file
            for test_file in test_files
        }
        for index, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if index == total or index % 20 == 0:
                print(
                    "Ground-truth progress: {}/{}".format(index, total),
                    flush=True,
                )
    return sorted(results, key=lambda item: item["test_file"])


def resource_environment_path(record, resource_root):
    project = record["repo"].replace("/", "__")
    issue = record["instance_id"].rsplit("-", 1)[-1]
    return Path(resource_root).resolve() / project / issue / "environment.yml"


def normalize_environment(source, environment_name, project, versions):
    """Rename an exported SWE-bench environment and remove its machine prefix."""
    lines = source.splitlines()
    normalized = []
    replaced_name = False
    for line in lines:
        if line.startswith("name:"):
            normalized.append("name: {}".format(environment_name))
            replaced_name = True
        elif line.startswith("prefix:"):
            continue
        else:
            normalized.append(line)
    if not replaced_name:
        raise ValueError("environment.yml has no top-level name")
    result = "\n".join(normalized).rstrip() + "\n"
    if project == "sympy/sympy":
        # SWE-bench invokes SymPy through bin/test, so its lock file does not
        # include pytest. NameRTS' dynamic import collector is a pytest plugin
        # and the per-file execution contract is ``pytest path/to/test.py``.
        marker = "  - pip:\n"
        if marker not in result:
            raise ValueError("SymPy environment has no pip dependency section")
        result = result.replace(
            marker, marker + "      - pytest==7.4.0\n", 1
        )
    if project == "astropy/astropy" and "3.1" in versions:
        # The SWE-bench lock is sufficient for its targeted tests, but NumPy
        # 1.24+ removes np.int while Astropy 3.1's compiled modules still use
        # it. The all-test-file workflow therefore needs the last compatible
        # NumPy series.
        old_numpy = "      - numpy==1.25.2\n"
        if old_numpy not in result:
            raise ValueError("Could not find the Astropy 3.1 NumPy pin")
        result = result.replace(old_numpy, "      - numpy==1.23.5\n", 1)
    return result


def command_generate_target_envs(args):
    records = load_subset(args.subset)
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    unique_sources = {}
    instance_sources = {}
    for record in records:
        source_path = resource_environment_path(record, args.resource_root)
        source = source_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        unique_sources.setdefault(
            digest, {"source": source, "source_paths": set(), "instances": []}
        )
        unique_sources[digest]["source_paths"].add(str(source_path))
        unique_sources[digest]["instances"].append(record)
        instance_sources[record["instance_id"]] = digest

    environment_records = {}
    instance_environments = {}
    for digest, source_record in sorted(unique_sources.items()):
        projects = sorted(set(item["repo"] for item in source_record["instances"]))
        if len(projects) != 1:
            raise ValueError(
                "One environment hash unexpectedly spans projects: {}".format(projects)
            )
        project_slug = re.sub(r"[^a-z0-9]+", "_", projects[0].split("/")[-1].lower())
        environment_name = "RTSTest39_{}_{}".format(project_slug, digest[:8])
        output_path = output_root / "{}.yml".format(environment_name)
        versions = sorted(
            set(item["version"] for item in source_record["instances"]),
            key=lambda value: tuple(int(part) for part in value.split(".")),
        )
        output_path.write_text(
            normalize_environment(
                source_record["source"], environment_name, projects[0], versions
            ),
            encoding="utf-8",
        )
        environment_records[environment_name] = {
            "environment_file": str(output_path),
            "project": projects[0],
            "resource_sha256": digest,
            "source_environment_files": sorted(source_record["source_paths"]),
            "versions": versions,
            "instances": sorted(
                item["instance_id"] for item in source_record["instances"]
            ),
            "local_augmentations": (
                ["pytest==7.4.0 for NameRTS per-file pytest execution"]
                if projects[0] == "sympy/sympy"
                else (
                    [
                        "numpy==1.23.5 for Astropy 3.1 all-test compatibility "
                        "(SWE-bench target-only lock used 1.25.2)"
                    ]
                    if projects[0] == "astropy/astropy" and "3.1" in versions
                    else []
                )
            ),
        }
        for record in source_record["instances"]:
            instance_environments[record["instance_id"]] = environment_name

    manifest = {
        "schema_version": 1,
        "subset": str(Path(args.subset).resolve()),
        "resource_root": str(Path(args.resource_root).resolve()),
        "environment_count": len(environment_records),
        "environments": environment_records,
        "instance_environments": dict(sorted(instance_environments.items())),
    }
    manifest_path = output_root / "environment_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        "Generated {} target environment files -> {}".format(
            len(environment_records), manifest_path
        )
    )


def execution_context(args):
    records = load_subset(args.subset)
    record = select_record(records, args.instance)
    environment_manifest = load_environment_manifest(args.environment_manifest)
    config = project_configuration(record, environment_manifest)
    return record, config


def reset_to_base(repo, base_commit):
    _run(["git", "reset", "--hard", base_commit], cwd=repo)
    conftest_path = Path(repo) / "conftest.py"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "conftest.py"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).returncode == 0
    if conftest_path.exists() and not tracked:
        contents = conftest_path.read_text(encoding="utf-8", errors="ignore")
        generated_markers = (
            'OUT_DIR = os.path.join(PROJECT_ROOT, "coverage")',
            "module_fixture_import_collector",
            "pytest_sessionfinish",
        )
        if all(marker in contents for marker in generated_markers):
            conftest_path.unlink()


def command_smoke(args):
    record, config = execution_context(args)
    repo = Path(config["repo"]).resolve()
    assert_clean_tracked_worktree(repo)
    try:
        reset_to_base(repo, record["base_commit"])
        install = install_project(
            record,
            config,
            Path(args.run_root)
            / config["slug"]
            / "installs"
            / ("smoke_" + record["instance_id"] + ".log"),
        )
        test_files = collect_test_files(repo)
        if not test_files:
            raise RuntimeError("No test files found by collect_all_heuristic")
        module_name = config.get("import_name", config["slug"])
        import_result = _run(
            [
                install["test_python"],
                "-c",
                "import {}; print({}.__file__)".format(module_name, module_name),
            ],
            cwd=repo,
            check=False,
        )
        def smoke_rank(test_file):
            fixture_penalty = int(
                any(
                    part in ("roots", "fixtures", "test-data", "test_data")
                    for part in Path(test_file).parts
                )
            )
            return (fixture_penalty, len(Path(test_file).parts), test_file)

        smoke_attempts = []
        collect_result = None
        smoke_file = None
        for candidate in sorted(test_files, key=smoke_rank)[:20]:
            if record["repo"] == "django/django":
                normalized = candidate.replace("\\", "/")
                if normalized.startswith("tests/"):
                    normalized = normalized[len("tests/"):]
                if normalized.endswith(".py"):
                    normalized = normalized[:-len(".py")]
                collect_command = [
                    install["test_python"],
                    "./tests/runtests.py",
                    "--verbosity",
                    "1",
                    "--settings=test_sqlite",
                    "--parallel",
                    "1",
                    normalized.replace("/", "."),
                ]
            else:
                collect_command = [
                    install["test_python"],
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    candidate,
                    "-p",
                    "no:cacheprovider",
                ]
            collect_result = _run(
                collect_command, cwd=repo, check=False, timeout=args.timeout
            )
            smoke_attempts.append(
                {
                    "test_file": candidate,
                    "return_code": collect_result.returncode,
                }
            )
            smoke_file = candidate
            if collect_result.returncode == 0:
                break
        result = {
            "base_commit": record["base_commit"],
            "instance_id": record["instance_id"],
            "project": record["repo"],
            "version": record["version"],
            "test_env": config["test_env"],
            "python": interpreter_version(install["test_python"]),
            "total_test_files": len(test_files),
            "smoke_test_file": smoke_file,
            "smoke_attempts": smoke_attempts,
            "import_return_code": import_result.returncode,
            "import_stdout": import_result.stdout.strip(),
            "import_stderr": import_result.stderr.strip(),
            "collect_return_code": collect_result.returncode,
            "collect_stdout": collect_result.stdout[-4000:],
            "collect_stderr": collect_result.stderr[-4000:],
            "safe": (
                import_result.returncode == 0 and collect_result.returncode == 0
            ),
        }
        output_path = (
            Path(args.run_root)
            / config["slug"]
            / "smoke"
            / (record["instance_id"] + ".json")
        )
        _write_json(output_path, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["safe"] else 1
    finally:
        reset_to_base(repo, record["base_commit"])


def command_ground_truth(args):
    helpers = shared_validation_helpers()
    record, config = execution_context(args)
    repo = Path(config["repo"]).resolve()
    patch_path = Path(record["patch_path"]).resolve()
    patch_text = patch_path.read_text(encoding="utf-8")
    changed_lines = helpers.parse_patch_changed_lines(patch_text)

    assert_clean_tracked_worktree(repo)
    try:
        reset_to_base(repo, record["base_commit"])
        install = install_project(
            record,
            config,
            Path(args.run_root)
            / config["slug"]
            / "installs"
            / (record["instance_id"] + ".log"),
        )
        _run(["git", "apply", "--check", str(patch_path)], cwd=repo)
        _run(["git", "apply", str(patch_path)], cwd=repo)
        patch_dependencies = PATCH_DEPENDENCIES.get(record["instance_id"], [])
        if patch_dependencies:
            _run(
                [install["test_python"], "-m", "pip", "install"]
                + patch_dependencies,
                cwd=repo,
            )
        functions, uninstrumentable = helpers.identify_modified_functions(
            repo, record["base_commit"], patch_text, changed_lines
        )
        if not functions:
            raise RuntimeError(
                "Patch has no instrumentable Python functions: {}".format(
                    patch_path
                )
            )
        helpers.instrument_functions(repo, functions)

        run_id = "{}_{}_{}".format(
            record["instance_id"], int(time.time()), os.getpid()
        )
        run_dir = (
            Path(args.run_root).resolve()
            / config["slug"]
            / "ground_truth_runs"
            / run_id
        )
        (run_dir / "hits").mkdir(parents=True)
        (run_dir / "details").mkdir(parents=True)
        (run_dir / "logs").mkdir(parents=True)
        _write_json(
            run_dir / "instrumentation.json",
            {
                "functions": functions,
                "uninstrumentable": uninstrumentable,
            },
        )

        test_files = collect_test_files(repo)
        print(
            "Running {} {} test files with {} workers in {}".format(
                record["instance_id"],
                len(test_files),
                args.workers,
                config["test_env"],
            ),
            flush=True,
        )
        started = time.time()
        test_results = run_tests(
            install["test_python"],
            repo,
            record["repo"],
            test_files,
            run_dir,
            args.workers,
            args.timeout,
            helpers,
        )
        all_affected = sorted(
            item["test_file"] for item in test_results if item["markers"]
        )
        affected = sorted(
            item["test_file"]
            for item in test_results
            if item["return_code"] == 0 and item["markers"]
        )
        in_test_affected = sorted(
            item["test_file"]
            for item in test_results
            if item["return_code"] == 0
            and item["markers"]
            and any(
                not detail.endswith("\t<outside-test>")
                for detail in item["details"]
            )
        )
        failed = sorted(
            item["test_file"]
            for item in test_results
            if item["return_code"] != 0 and not item["timed_out"]
        )
        timed_out = sorted(
            item["test_file"] for item in test_results if item["timed_out"]
        )
        result_record = {
            "base_commit": record["base_commit"],
            "instance_id": record["instance_id"],
            "patch_file": record["patch_file"],
            "project": record["repo"],
            "version": record["version"],
            "test_env": config["test_env"],
            "python": interpreter_version(install["test_python"]),
            "modified_functions": functions,
            "uninstrumentable_changes": uninstrumentable,
            "dynamic_hit_tests_all": all_affected,
            "dynamic_hit_tests": all_affected,
            "passing_dynamic_hit_tests": affected,
            "in_test_hit_tests": in_test_affected,
            "tests_to_run": all_affected,
            "affected_functions_by_test": {
                item["test_file"]: item["markers"]
                for item in test_results
                if item["markers"]
            },
            "total_tests": len(test_files),
            "passed_test_files": len(test_files) - len(failed) - len(timed_out),
            "failed_test_files": failed,
            "timed_out_test_files": timed_out,
            "elapsed_seconds": round(time.time() - started, 3),
            "run_dir": str(run_dir),
        }
        update_json_records(args.output, result_record)
        _write_json(run_dir / "test_results.json", test_results)
        print(json.dumps(result_record, indent=2, sort_keys=True))
        return 0
    finally:
        reset_to_base(repo, record["base_commit"])


def refine_ground_truth_record(record):
    """Reclassify an existing run without repeating any test execution."""
    results_path = Path(record["run_dir"]) / "test_results.json"
    test_results = json.loads(results_path.read_text(encoding="utf-8"))
    all_affected = sorted(
        item["test_file"] for item in test_results if item["markers"]
    )
    affected = sorted(
        item["test_file"]
        for item in test_results
        if item["return_code"] == 0 and item["markers"]
    )
    in_test_affected = sorted(
        item["test_file"]
        for item in test_results
        if item["return_code"] == 0
        and item["markers"]
        and any(
            not detail.endswith("\t<outside-test>") for detail in item["details"]
        )
    )
    record["dynamic_hit_tests_all"] = all_affected
    record["dynamic_hit_tests"] = affected
    record["in_test_hit_tests"] = in_test_affected
    record["tests_to_run"] = affected
    return record


def command_refine_ground_truth(args):
    paths = (
        [Path(path).resolve() for path in args.paths]
        if args.paths
        else [
            project_ground_truth_path(PROJECTS[project]["slug"])
            for project in sorted(PROJECTS)
        ]
    )
    total = 0
    for path in paths:
        if not path.exists():
            continue
        records = [
            refine_ground_truth_record(record)
            for record in _load_json_list(path)
        ]
        _write_json(path, records)
        total += len(records)
    print("Refined {} ground-truth records".format(total))


def command_apply_semantic_reviews(args):
    reviews = {
        item["instance_id"]: item for item in _load_json_list(args.reviews)
    }
    paths = (
        [Path(path).resolve() for path in args.paths]
        if args.paths
        else [
            project_ground_truth_path(PROJECTS[project]["slug"])
            for project in sorted(PROJECTS)
        ]
    )
    reviewed = 0
    for path in paths:
        if not path.exists():
            continue
        records = _load_json_list(path)
        changed = False
        for record in records:
            review = reviews.get(record["instance_id"])
            if not review:
                continue
            dynamic_hits = set(record.get("dynamic_hit_tests", []))
            if review.get("exclude_all_dynamic_hits"):
                excluded = sorted(dynamic_hits)
            elif review.get("keep_tests") is not None:
                excluded = sorted(
                    dynamic_hits - set(review.get("keep_tests", []))
                )
            else:
                excluded = sorted(
                    dynamic_hits & set(review.get("exclude_tests", []))
                )
            included = sorted(
                set(review.get("include_tests", [])) & dynamic_hits
            )
            final_tests = (
                set(record.get("tests_to_run", dynamic_hits))
                - set(excluded)
            ) | set(included)
            record["tests_to_run"] = sorted(final_tests)
            record["semantic_excluded_tests"] = excluded
            record["semantic_review"] = {
                key: value
                for key, value in review.items()
                if key not in (
                    "exclude_tests",
                    "include_tests",
                    "keep_tests",
                )
            }
            changed = True
            reviewed += 1
        if changed:
            _write_json(path, records)
    print("Applied {} semantic reviews".format(reviewed))


def command_summarize(args):
    """Combine reviewed ground truth with the latest comparison per instance."""
    records = _load_json_list(args.ground_truth)
    run_root = Path(args.run_root).resolve()
    instances = []
    for record in records:
        comparison_paths = list(
            run_root.glob(
                "*/comparisons/namerts_{}_*.json".format(
                    record["instance_id"]
                )
            )
        )
        if not comparison_paths:
            raise RuntimeError(
                "No NameRTS comparison found for {}".format(
                    record["instance_id"]
                )
            )
        comparison_path = max(
            comparison_paths,
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        with comparison_path.open(
            "r", encoding="utf-8"
        ) as comparison_file:
            comparison = json.load(comparison_file)
        dynamic_hits = sorted(
            set(
                record.get(
                    "dynamic_hit_tests",
                    record.get("tests_to_run", []),
                )
            )
        )
        final_ground_truth = sorted(set(record.get("tests_to_run", [])))
        selected = sorted(set(comparison.get("tests_to_run", [])))
        missing = sorted(set(final_ground_truth) - set(selected))
        instances.append(
            {
                "base_commit": record["base_commit"],
                "comparison": str(comparison_path),
                "dynamic_hit_tests": dynamic_hits,
                "failed_test_files": record.get("failed_test_files", []),
                "final_ground_truth": final_ground_truth,
                "instance_id": record["instance_id"],
                "missing_tests": missing,
                "passed_test_files": record.get("passed_test_files", 0),
                "project": record["project"],
                "python": record.get("python"),
                "safe": not missing,
                "semantic_excluded_tests": record.get(
                    "semantic_excluded_tests", []
                ),
                "semantic_review": record.get("semantic_review"),
                "tests_to_run": selected,
                "timed_out_test_files": record.get(
                    "timed_out_test_files", []
                ),
                "total_test_files": record.get("total_tests", 0),
                "version": record["version"],
            }
        )

    project_summaries = {}
    for project in sorted(PROJECTS):
        project_instances = [
            item for item in instances if item["project"] == project
        ]
        if not project_instances:
            continue
        project_summaries[project] = {
            "dynamic_hit_test_files": sum(
                len(item["dynamic_hit_tests"])
                for item in project_instances
            ),
            "final_ground_truth_test_files": sum(
                len(item["final_ground_truth"])
                for item in project_instances
            ),
            "instances": len(project_instances),
            "missing_test_files": sum(
                len(item["missing_tests"]) for item in project_instances
            ),
            "safe_instances": sum(
                1 for item in project_instances if item["safe"]
            ),
            "selected_test_files": sum(
                len(item["tests_to_run"]) for item in project_instances
            ),
            "versions": sorted(
                set(item["version"] for item in project_instances)
            ),
        }

    aggregate = {
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
        "missing_test_files": sum(
            len(item["missing_tests"]) for item in instances
        ),
        "passed_test_file_runs": sum(
            item["passed_test_files"] for item in instances
        ),
        "safe_instances": sum(1 for item in instances if item["safe"]),
        "selected_test_files": sum(
            len(item["tests_to_run"]) for item in instances
        ),
        "semantic_excluded_test_files": sum(
            len(item["semantic_excluded_tests"]) for item in instances
        ),
        "timed_out_test_file_runs": sum(
            len(item["timed_out_test_files"]) for item in instances
        ),
        "total_test_file_runs": sum(
            item["total_test_files"] for item in instances
        ),
        "unsafe_instances": sum(
            1 for item in instances if not item["safe"]
        ),
    }
    summary = {
        "aggregate": aggregate,
        "instances": sorted(
            instances, key=lambda item: item["instance_id"]
        ),
        "projects": project_summaries,
    }
    _write_json(args.output, summary)
    print(json.dumps(aggregate, indent=2, sort_keys=True))
    return 0


def command_namerts(args):
    from src.evaluate import CACHED_FILES_NBDP, evaluate_instance, install_instrumentor
    from src.namebdp import NameBDP
    import src.config as namerts_config

    namerts_config.PER_FILE_TEST_TIMEOUT = args.timeout

    record, config = execution_context(args)
    repo = Path(config["repo"]).resolve()
    patch_path = Path(record["patch_path"]).resolve()
    matching_ground_truth = [
        item
        for item in _load_json_list(args.ground_truth)
        if item.get("instance_id") == record["instance_id"]
    ]
    if len(matching_ground_truth) != 1:
        raise RuntimeError(
            "Expected one ground-truth record for {}, found {}".format(
                record["instance_id"], len(matching_ground_truth)
            )
        )

    assert_clean_tracked_worktree(repo)
    try:
        reset_to_base(repo, record["base_commit"])
        install_project(
            record,
            config,
            Path(args.run_root)
            / config["slug"]
            / "installs"
            / ("namerts_" + record["instance_id"] + ".log"),
        )
        install_instrumentor(config["test_env"])
        result = evaluate_instance(
            direct_parent=record["base_commit"],
            true_parent=record["base_commit"],
            current=None,
            target=True,
            tool_name="NameBDP",
            repo_path=str(repo),
            conda_env=config["test_env"],
            tool_class=NameBDP,
            cached_files=CACHED_FILES_NBDP,
            time_tag="python39_{}_{}_{}".format(
                config["slug"], record["instance_id"], time.time()
            ),
            n=args.workers,
            registry_decorator_keywords=set(config["registry_decorators"]),
            use_isolation=False,
            run_parent=True,
            patch_path=str(patch_path),
            run_current_tests=False,
            reuse_parent_cache=args.reuse_parent_cache,
        )
        selected = sorted(
            set(Path(path).as_posix() for path in result["tests_to_run"])
        )
        affected = sorted(
            set(
                Path(path).as_posix()
                for path in matching_ground_truth[0]["tests_to_run"]
            )
        )
        missing = sorted(set(affected) - set(selected))
        comparison = {
            "base_commit": record["base_commit"],
            "ground_truth": affected,
            "instance_id": record["instance_id"],
            "missing_tests": missing,
            "namerts_result": result,
            "patch_file": record["patch_file"],
            "project": record["repo"],
            "safe": not missing,
            "test_env": config["test_env"],
            "tests_to_run": selected,
            "version": record["version"],
        }
        output_path = (
            Path(args.run_root).resolve()
            / config["slug"]
            / "comparisons"
            / ("namerts_{}_{}.json".format(record["instance_id"], int(time.time())))
        )
        _write_json(output_path, comparison)
        comparison["output"] = str(output_path)
        print(json.dumps(comparison, indent=2, sort_keys=True))
        return 0 if not missing else 1
    finally:
        reset_to_base(repo, record["base_commit"])


def project_ground_truth_path(project_slug):
    return (
        Path(__file__).resolve().parent.parent
        / "ground truth"
        / "python39"
        / ("gt_{}.json".format(project_slug))
    )


def command_batch_project(args):
    records = [
        record
        for record in load_subset(args.subset)
        if record["repo"] == args.project
        and (
            not args.versions
            or str(record["version"]) in set(args.versions)
        )
    ]
    if not records:
        raise ValueError("No subset records for project {}".format(args.project))
    slug = PROJECTS[args.project]["slug"]
    ground_truth_path = (
        Path(args.ground_truth).resolve()
        if args.ground_truth
        else project_ground_truth_path(slug)
    )
    status_path = (
        Path(args.status).resolve()
        if args.status
        else Path(args.run_root).resolve() / slug / "batch_status.json"
    )
    completed_ground_truth = {
        item["instance_id"] for item in _load_json_list(ground_truth_path)
    }
    status_by_instance = {
        item["instance_id"]: item for item in _load_json_list(status_path)
    }

    for index, record in enumerate(records, start=1):
        instance_id = record["instance_id"]
        comparisons = list(
            (
                Path(args.run_root).resolve()
                / slug
                / "comparisons"
            ).glob("namerts_{}_*.json".format(instance_id))
        )
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
                "project": record["repo"],
                "version": record["version"],
            },
        )
        state["batch_index"] = index
        state["batch_total"] = len(records)
        if not need_ground_truth and not need_namerts:
            state["status"] = "skipped_completed"
            _write_json(
                status_path,
                sorted(
                    status_by_instance.values(),
                    key=lambda item: item["instance_id"],
                ),
            )
            continue
        print(
            "[{}] {}/{} {}".format(slug, index, len(records), instance_id),
            flush=True,
        )
        try:
            common = {
                "environment_manifest": args.environment_manifest,
                "instance": instance_id,
                "run_root": args.run_root,
                "subset": args.subset,
            }
            if need_ground_truth:
                gt_args = argparse.Namespace(
                    output=str(ground_truth_path),
                    timeout=args.timeout,
                    workers=args.ground_truth_workers,
                    **common
                )
                state["ground_truth_return_code"] = command_ground_truth(gt_args)
                completed_ground_truth.add(instance_id)
            if need_namerts:
                namerts_args = argparse.Namespace(
                    ground_truth=str(ground_truth_path),
                    reuse_parent_cache=args.reuse_parent_cache,
                    timeout=args.timeout,
                    workers=args.namerts_workers,
                    **common
                )
                state["namerts_return_code"] = command_namerts(namerts_args)
            state["status"] = "completed"
            state.pop("error", None)
        except Exception as error:
            state["status"] = "failed"
            state["error"] = "{}: {}".format(type(error).__name__, error)
            print(
                "[{}] FAILED {}: {}".format(slug, instance_id, state["error"]),
                file=sys.stderr,
                flush=True,
            )
            if not args.continue_on_error:
                raise
        finally:
            _write_json(
                status_path,
                sorted(
                    status_by_instance.values(),
                    key=lambda item: item["instance_id"],
                ),
            )
    return 0


def command_smoke_project(args):
    records = [
        record
        for record in load_subset(args.subset)
        if record["repo"] == args.project
    ]
    slug = PROJECTS[args.project]["slug"]
    results = []
    for index, record in enumerate(records, start=1):
        print(
            "[{} smoke] {}/{} {}".format(
                slug, index, len(records), record["instance_id"]
            ),
            flush=True,
        )
        smoke_args = argparse.Namespace(
            environment_manifest=args.environment_manifest,
            instance=record["instance_id"],
            run_root=args.run_root,
            subset=args.subset,
            timeout=args.timeout,
        )
        try:
            return_code = command_smoke(smoke_args)
            results.append(
                {
                    "instance_id": record["instance_id"],
                    "return_code": return_code,
                    "safe": return_code == 0,
                }
            )
        except Exception as error:
            results.append(
                {
                    "error": "{}: {}".format(type(error).__name__, error),
                    "instance_id": record["instance_id"],
                    "return_code": 1,
                    "safe": False,
                }
            )
            if not args.continue_on_error:
                raise
    output_path = (
        Path(args.run_root).resolve() / slug / "smoke" / "summary.json"
    )
    _write_json(output_path, results)
    failures = [item for item in results if not item["safe"]]
    print(
        "[{} smoke] {} total, {} failed".format(
            slug, len(results), len(failures)
        ),
        flush=True,
    )
    return 1 if failures else 0


def command_merge_ground_truth(args):
    records = []
    for project in sorted(PROJECTS):
        path = project_ground_truth_path(PROJECTS[project]["slug"])
        records.extend(_load_json_list(path))
    records.sort(key=lambda item: item["instance_id"])
    _write_json(args.output, records)
    print("Merged {} ground-truth records -> {}".format(len(records), args.output))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    subset_parser = subparsers.add_parser(
        "select-subset", help="create the deterministic project/version subset"
    )
    subset_parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    subset_parser.add_argument("--output", default=str(DEFAULT_SUBSET))
    subset_parser.add_argument("--seed", default=SELECTION_SEED)
    subset_parser.set_defaults(func=command_select_subset)
    validation_subset_parser = subparsers.add_parser(
        "filter-subset",
        help="materialize an exact execution subset using version filters",
    )
    validation_subset_parser.add_argument(
        "--subset", default=str(DEFAULT_SUBSET)
    )
    validation_subset_parser.add_argument(
        "--output", default=str(DEFAULT_VALIDATED_SUBSET)
    )
    validation_subset_parser.add_argument(
        "--project-versions",
        nargs="*",
        default=[],
        metavar="PROJECT=VERSION[,VERSION]",
    )
    validation_subset_parser.set_defaults(func=command_filter_subset)
    env_parser = subparsers.add_parser(
        "generate-target-envs",
        help="copy and normalize unique SWE-bench environments for the subset",
    )
    env_parser.add_argument("--subset", default=str(DEFAULT_SUBSET))
    env_parser.add_argument("--resource-root", default=str(DEFAULT_RESOURCE_ROOT))
    env_parser.add_argument("--output-dir", default=str(DEFAULT_ENV_ROOT))
    env_parser.set_defaults(func=command_generate_target_envs)

    def add_execution_arguments(command_parser):
        command_parser.add_argument("--instance", required=True)
        command_parser.add_argument("--subset", default=str(DEFAULT_SUBSET))
        command_parser.add_argument(
            "--environment-manifest", default=str(DEFAULT_ENV_MANIFEST)
        )
        command_parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))

    smoke_parser = subparsers.add_parser(
        "smoke", help="install one instance and collect one test file"
    )
    add_execution_arguments(smoke_parser)
    smoke_parser.add_argument("--timeout", type=int, default=300)
    smoke_parser.set_defaults(func=command_smoke)

    smoke_project_parser = subparsers.add_parser(
        "smoke-project",
        help="smoke every selected version of one project sequentially",
    )
    smoke_project_parser.add_argument(
        "--project", required=True, choices=sorted(PROJECTS)
    )
    smoke_project_parser.add_argument("--subset", default=str(DEFAULT_SUBSET))
    smoke_project_parser.add_argument(
        "--environment-manifest", default=str(DEFAULT_ENV_MANIFEST)
    )
    smoke_project_parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    smoke_project_parser.add_argument("--timeout", type=int, default=300)
    smoke_project_parser.add_argument("--continue-on-error", action="store_true")
    smoke_project_parser.set_defaults(func=command_smoke_project)

    gt_parser = subparsers.add_parser(
        "ground-truth", help="collect dynamic function-hit tests for one instance"
    )
    add_execution_arguments(gt_parser)
    gt_parser.add_argument("--output", default=str(DEFAULT_GROUND_TRUTH))
    gt_parser.add_argument("--workers", type=int, default=24)
    gt_parser.add_argument("--timeout", type=int, default=900)
    gt_parser.set_defaults(func=command_ground_truth)

    namerts_parser = subparsers.add_parser(
        "namerts", help="build parent cache and compare patched selection"
    )
    add_execution_arguments(namerts_parser)
    namerts_parser.add_argument(
        "--ground-truth", default=str(DEFAULT_GROUND_TRUTH)
    )
    namerts_parser.add_argument("--workers", type=int, default=32)
    namerts_parser.add_argument("--timeout", type=int, default=900)
    namerts_parser.add_argument("--reuse-parent-cache", action="store_true")
    namerts_parser.set_defaults(func=command_namerts)

    batch_parser = subparsers.add_parser(
        "batch-project",
        help="sequentially validate every selected version for one project",
    )
    batch_parser.add_argument("--project", required=True, choices=sorted(PROJECTS))
    batch_parser.add_argument(
        "--versions",
        nargs="+",
        help="optionally run only these manifest version labels",
    )
    batch_parser.add_argument("--subset", default=str(DEFAULT_SUBSET))
    batch_parser.add_argument(
        "--environment-manifest", default=str(DEFAULT_ENV_MANIFEST)
    )
    batch_parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    batch_parser.add_argument("--ground-truth")
    batch_parser.add_argument("--status")
    batch_parser.add_argument("--ground-truth-workers", type=int, default=24)
    batch_parser.add_argument("--namerts-workers", type=int, default=32)
    batch_parser.add_argument("--timeout", type=int, default=900)
    batch_parser.add_argument("--force-ground-truth", action="store_true")
    batch_parser.add_argument("--force-namerts", action="store_true")
    batch_parser.add_argument("--ground-truth-only", action="store_true")
    batch_parser.add_argument("--namerts-only", action="store_true")
    batch_parser.add_argument("--reuse-parent-cache", action="store_true")
    batch_parser.add_argument("--continue-on-error", action="store_true")
    batch_parser.set_defaults(func=command_batch_project)

    merge_parser = subparsers.add_parser(
        "merge-ground-truth", help="merge the four concurrency-safe project files"
    )
    merge_parser.add_argument("--output", default=str(DEFAULT_GROUND_TRUTH))
    merge_parser.set_defaults(func=command_merge_ground_truth)

    refine_parser = subparsers.add_parser(
        "refine-ground-truth",
        help="exclude nonzero test files while retaining all-hit diagnostics",
    )
    refine_parser.add_argument("--paths", nargs="*")
    refine_parser.set_defaults(func=command_refine_ground_truth)

    review_parser = subparsers.add_parser(
        "apply-semantic-reviews",
        help="replay evidence-backed removals from dynamic-hit ground truth",
    )
    review_parser.add_argument(
        "--reviews",
        default=str(
            Path(__file__).resolve().parent.parent
            / "ground truth"
            / "python39"
            / "semantic_reviews.json"
        ),
    )
    review_parser.add_argument("--paths", nargs="*")
    review_parser.set_defaults(func=command_apply_semantic_reviews)

    summary_parser = subparsers.add_parser(
        "summarize",
        help="summarize reviewed ground truth and latest comparisons",
    )
    summary_parser.add_argument(
        "--ground-truth", default=str(DEFAULT_GROUND_TRUTH)
    )
    summary_parser.add_argument(
        "--run-root", default=str(DEFAULT_RUN_ROOT)
    )
    summary_parser.add_argument(
        "--output", default=str(DEFAULT_FINAL_SUMMARY)
    )
    summary_parser.set_defaults(func=command_summarize)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.error("a command is required")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
