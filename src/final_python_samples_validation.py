"""Final cross-version validation for curated SWE-bench Verified samples.

This module intentionally stays compatible with Python 3.6 and newer. It
reuses the established per-file ground-truth and NameRTS workflow while
keeping every destructive Git operation inside dedicated runtime worktrees.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path("/shared_dir/swebench_verified_repo_python_samples")
DEFAULT_MANIFEST = DATA_ROOT / "manifest.tsv"
DEFAULT_SUBSET = DATA_ROOT / "selected_subset.json"
RESOURCE_ROOT = Path(
    "/shared_dir/swe-bench-repos/SWE-bench/swebench/resources/swebench-og"
)
REPOS_ROOT = Path("/shared_dir/swe-bench-repos")
ENV_ROOT = PROJECT_ROOT / "environments" / "final_python_samples"
ENV_MANIFEST = ENV_ROOT / "environment_manifest.json"
RUN_ROOT = PROJECT_ROOT / "runtime" / "final_python_samples"
WORKTREE_ROOT = RUN_ROOT / "worktrees"
GROUND_TRUTH_ROOT = PROJECT_ROOT / "ground truth" / "final_python_samples"
SUMMARY_PATH = RUN_ROOT / "final_summary.json"
ENVIRONMENT_VERIFICATION_PATH = RUN_ROOT / "environment_verification.json"
MISSING_REVIEW_PATH = RUN_ROOT / "missing_review.json"


PROJECTS = {
    "astropy/astropy": {
        "slug": "astropy",
        "import_name": "astropy",
        "registry_decorators": [],
    },
    "django/django": {
        "slug": "django",
        "import_name": "django",
        "registry_decorators": [],
    },
    "matplotlib/matplotlib": {
        "slug": "matplotlib",
        "import_name": "matplotlib",
        "registry_decorators": ["export"],
    },
    "mwaskom/seaborn": {
        "slug": "seaborn",
        "import_name": "seaborn",
        "registry_decorators": [],
    },
    "pallets/flask": {
        "slug": "flask",
        "import_name": "flask",
        "registry_decorators": [],
    },
    "psf/requests": {
        "slug": "requests",
        "import_name": "requests",
        "registry_decorators": [],
    },
    "pydata/xarray": {
        "slug": "xarray",
        "import_name": "xarray",
        "registry_decorators": [],
    },
    "pylint-dev/pylint": {
        "slug": "pylint",
        "import_name": "pylint",
        "registry_decorators": [],
    },
    "pytest-dev/pytest": {
        "slug": "pytest",
        "import_name": "pytest",
        "registry_decorators": [],
    },
    "scikit-learn/scikit-learn": {
        "slug": "scikit_learn",
        "import_name": "sklearn",
        "registry_decorators": [],
    },
    "sphinx-doc/sphinx": {
        "slug": "sphinx",
        "import_name": "sphinx",
        "registry_decorators": [],
    },
    "sympy/sympy": {
        "slug": "sympy",
        "import_name": "sympy",
        "registry_decorators": ["register"],
    },
}

NAMERTS_ENVIRONMENTS = {
    "3.6": "NameRTS36",
    "3.7": "NameRTS37",
    "3.8": "NameRTS38",
    "3.9": "NameRTS39",
    "3.10": "NameRTS310",
    "3.11": "NameRTS311",
}


MISSING_CLASSIFICATIONS = {
    "astropy__astropy-7166": {
        "category": "import_or_failed_test_entry_hit",
        "version_related": False,
        "reason": (
            "The passing miss triggers InheritDocstrings.__init__ before pytest "
            "starts its test; two additional hits come from failed astropy_helpers "
            "files. None indicates a Python parser or bytecode failure."
        ),
    },
    "django__django-10554": {
        "category": "test_runner_setup_false_positive",
        "version_related": False,
        "reason": (
            "The missing file contains only a pass test. SQLCompiler.get_order_by "
            "runs while Django prepares the test database, not because the test "
            "uses the patched UNION ordering behavior."
        ),
    },
    "django__django-13809": {
        "category": "unchanged_default_path",
        "version_related": False,
        "reason": (
            "The file builds the runserver parser, but the patch changes behavior "
            "only when the new --skip-checks option is supplied."
        ),
    },
    "django__django-14725": {
        "category": "unchanged_default_path",
        "version_related": False,
        "reason": (
            "The missing files call model formset factories through their existing "
            "edit_only=False path; the new edit-only behavior is not requested."
        ),
    },
    "django__django-16454": {
        "category": "dynamic_inherited_dispatch",
        "version_related": False,
        "reason": (
            "CommandParser.add_subparsers is a new override of argparse behavior. "
            "The parent cache points at the external inherited method, so the new "
            "project method has no parent name edge to propagate."
        ),
    },
    "matplotlib__matplotlib-14623": {
        "category": "entry_hit_without_changed_branch_confirmation",
        "version_related": False,
        "reason": (
            "The files call axis-limit and locator entry points, mostly through "
            "plot construction. The patch changes descending-limit behavior; "
            "entry-only instrumentation does not prove that branch was observed."
        ),
    },
    "matplotlib__matplotlib-13989": {
        "category": "failed_test_entry_hit",
        "version_related": False,
        "reason": (
            "All missing entry hits are from target test files that fail in the "
            "historical Matplotlib environment; NameRTS itself completes normally."
        ),
    },
    "matplotlib__matplotlib-22719": {
        "category": "failed_test_entry_hit",
        "version_related": False,
        "reason": (
            "The only missing hit is from a failed target test file. Generated "
            "FreeType Python 2 tools were compileall noise and are now excluded."
        ),
    },
    "mwaskom__seaborn-3069": {
        "category": "failed_test_entry_hit",
        "version_related": False,
        "reason": (
            "All missing hits are in target test files with nonzero exits; the "
            "NameRTS Python 3.9 cache and selection phases complete normally."
        ),
    },
    "pallets__flask-5014": {
        "category": "failed_test_entry_hit",
        "version_related": False,
        "reason": (
            "The missing entry hit is in a failed target test file; NameRTS on "
            "Python 3.11 reports no interpreter or parser error."
        ),
    },
    "psf__requests-1142": {
        "category": "failed_historical_network_suite",
        "version_related": False,
        "reason": (
            "Requests 1.1 has one root-level test module and its live-network suite "
            "fails, while collection, instrumentation, and NameRTS all succeed."
        ),
    },
    "pytest-dev__pytest-10051": {
        "category": "failed_test_entry_hit",
        "version_related": False,
        "reason": (
            "The missing entry hit is in a failed target test file; pytest 7.2 "
            "build metadata and NameRTS Python 3.9 processing both succeed."
        ),
    },
    "scikit-learn__scikit-learn-25102": {
        "category": "unchanged_default_path",
        "version_related": False,
        "reason": (
            "The missing files call BaseEstimator._validate_data with the existing "
            "cast_to_ndarray=True behavior. The patch-specific false path is used "
            "by SelectorMixin pandas output, not these modules."
        ),
    },
    "sympy__sympy-11618": {
        "category": "behavior_branch_not_reached",
        "version_related": False,
        "reason": (
            "The prior node-level review passed all 36 candidate tests and observed "
            "no execution of the patch-only mixed-type, mixed-dimension branch."
        ),
    },
}


def _run(command, cwd=None, check=True, env=None):
    process = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
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
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_manifest(path=DEFAULT_MANIFEST):
    manifest_path = Path(path).resolve()
    records = []
    with manifest_path.open(
        "r", encoding="utf-8", newline=""
    ) as manifest_file:
        for row in csv.DictReader(manifest_file, delimiter="\t"):
            record = dict(row)
            # python39_validation uses ``version`` internally.
            record["version"] = record["project_version"]
            record["patch_path"] = str(
                (manifest_path.parent / record["patch_file"]).resolve()
            )
            records.append(record)
    return records


def load_subset(path=DEFAULT_SUBSET):
    with Path(path).resolve().open("r", encoding="utf-8") as subset_file:
        return json.load(subset_file)["instances"]


def resource_environment_path(record):
    project = record["repo"].replace("/", "__")
    issue = record["instance_id"].rsplit("-", 1)[-1]
    return RESOURCE_ROOT / project / issue / "environment.yml"


def command_prepare(args):
    records = load_manifest(args.manifest)
    selected = [
        record
        for record in records
        if record["python_version"] not in set(args.exclude_python)
    ]
    excluded = [
        {
            "instance_id": record["instance_id"],
            "python_version": record["python_version"],
            "reason": "Python 3.5 remains outside the agreed validation scope.",
        }
        for record in records
        if record not in selected
    ]
    document = {
        "schema_version": 1,
        "source_manifest": str(Path(args.manifest).resolve()),
        "manifest_record_count": len(records),
        "selection_strategy": (
            "Keep every manifest row except explicitly excluded Python "
            "versions; no random sampling."
        ),
        "selected_count": len(selected),
        "excluded": excluded,
        "python_version_counts": dict(
            sorted(Counter(r["python_version"] for r in selected).items())
        ),
        "project_counts": dict(
            sorted(Counter(r["repo"] for r in selected).items())
        ),
        "instances": selected,
    }
    _write_json(args.output, document)
    print(
        "Selected {} of {} instances -> {}".format(
            len(selected), len(records), Path(args.output).resolve()
        )
    )
    return 0


def normalized_environment(source, environment_name, record):
    project = record["repo"]
    output = []
    found_name = False
    for line in source.splitlines():
        if line.startswith("name:"):
            output.append("name: {}".format(environment_name))
            found_name = True
        elif line.startswith("prefix:"):
            continue
        else:
            output.append(line)
    if not found_name:
        raise ValueError("SWE-bench environment has no top-level name")
    text = "\n".join(output).rstrip() + "\n"
    augmentations = []
    if project == "sympy/sympy" and "pytest==" not in text:
        marker = "  - pip:\n"
        if marker not in text:
            raise ValueError("SymPy environment has no pip section")
        text = text.replace(
            marker, marker + "      - pytest==7.4.0\n", 1
        )
        augmentations.append(
            "pytest==7.4.0 for NameRTS one-test-file execution"
        )
    if (
        project == "matplotlib/matplotlib"
        and record["version"] in ("3.0", "3.1")
    ):
        marker = "  - pip:\n"
        if marker not in text:
            raise ValueError("Matplotlib environment has no pip section")
        text = text.replace(
            marker,
            "  - freetype=2.12\n  - pkg-config\n" + marker,
            1,
        )
        augmentations.append(
            "freetype=2.12 and pkg-config replace SWE-bench Docker's "
            "libfreetype6-dev/pkg-config apt prerequisites"
        )
    return text, augmentations


def existing_exact_environments():
    """Return source-resource hashes already materialized by earlier stages."""
    mapping = {}
    previous = (
        PROJECT_ROOT
        / "environments"
        / "python39_targets"
        / "environment_manifest.json"
    )
    if not previous.exists():
        return mapping
    document = json.loads(previous.read_text(encoding="utf-8"))
    for name, record in document.get("environments", {}).items():
        digest = record.get("resource_sha256")
        prefix = Path("/root/anaconda3/envs") / name
        if digest and prefix.is_dir():
            mapping[digest] = name
    return mapping


def command_generate_target_envs(args):
    records = load_subset(args.subset)
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    existing = existing_exact_environments()
    environments = {}
    instance_environments = {}
    for record in records:
        source_path = resource_environment_path(record)
        source = source_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        slug = PROJECTS[record["repo"]]["slug"]
        version_digits = record["python_version"].replace(".", "")
        environment_name = existing.get(
            digest,
            "RTSFinal{}_{}_{}".format(
                version_digits, slug, digest[:8]
            ),
        )
        normalized, augmentations = normalized_environment(
            source, environment_name, record
        )
        output_path = output_root / (environment_name + ".yml")
        output_path.write_text(normalized, encoding="utf-8")
        environments[environment_name] = {
            "environment_file": str(output_path),
            "instances": [record["instance_id"]],
            "local_augmentations": augmentations,
            "project": record["repo"],
            "python_version": record["python_version"],
            "resource_sha256": digest,
            "reused_existing_environment": environment_name in existing.values(),
            "source_environment_file": str(source_path),
        }
        instance_environments[record["instance_id"]] = environment_name
    manifest = {
        "schema_version": 1,
        "subset": str(Path(args.subset).resolve()),
        "environment_count": len(environments),
        "environments": environments,
        "instance_environments": dict(sorted(instance_environments.items())),
        "namerts_environments": NAMERTS_ENVIRONMENTS,
    }
    _write_json(output_root / "environment_manifest.json", manifest)
    print(
        "Generated {} target environment files -> {}".format(
            len(environments), output_root / "environment_manifest.json"
        )
    )
    return 0


def source_repo(record):
    return REPOS_ROOT / record["repo"].split("/")[-1]


def worktree_for_project(project):
    return WORKTREE_ROOT / PROJECTS[project]["slug"]


def command_create_worktrees(args):
    records = load_subset(args.subset)
    first_by_project = {}
    for record in records:
        first_by_project.setdefault(record["repo"], record)
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    for project, record in sorted(first_by_project.items()):
        source = source_repo(record).resolve()
        target = worktree_for_project(project).resolve()
        if (target / ".git").exists():
            head = _run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip()
            status = "reused"
        else:
            if target.exists() and any(target.iterdir()):
                raise RuntimeError(
                    "Refusing to replace non-empty worktree target {}".format(
                        target
                    )
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            _run(
                [
                    "git",
                    "-c",
                    "safe.directory={}".format(source),
                    "worktree",
                    "add",
                    "--detach",
                    str(target),
                    record["base_commit"],
                ],
                cwd=source,
            )
            head = record["base_commit"]
            status = "created"
        results.append(
            {
                "head": head,
                "project": project,
                "source": str(source),
                "status": status,
                "worktree": str(target),
            }
        )
    _write_json(RUN_ROOT / "worktrees.json", results)
    print("Prepared {} dedicated worktrees".format(len(results)))
    return 0


def configure_common_module():
    from src import python39_validation as common

    additions = {}
    for project, project_config in PROJECTS.items():
        config = dict(project_config)
        config["repo"] = str(worktree_for_project(project).resolve())
        additions[project] = config
    common.PROJECTS.update(additions)
    return common


def ground_truth_path(project):
    return GROUND_TRUTH_ROOT / (
        "gt_{}.json".format(PROJECTS[project]["slug"])
    )


def command_smoke(args):
    common = configure_common_module()
    return common.command_smoke(args)


def command_ground_truth(args):
    common = configure_common_module()
    return common.command_ground_truth(args)


def command_namerts(args):
    common = configure_common_module()
    return common.command_namerts(args)


def conda_environment_names():
    output = _run(["conda", "env", "list"]).stdout
    names = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(stripped.split()[0])
    return names


def command_create_environments(args):
    manifest = json.loads(
        Path(args.environment_manifest).read_text(encoding="utf-8")
    )
    existing = conda_environment_names()
    tasks = []
    for name, record in manifest["environments"].items():
        if name not in existing:
            tasks.append((name, record["environment_file"]))
    for version, name in NAMERTS_ENVIRONMENTS.items():
        if name in existing:
            continue
        environment_file = (
            PROJECT_ROOT / "environment_{}.yml".format(version.replace(".", ""))
        )
        tasks.append((name, str(environment_file)))

    def create_one(item):
        name, environment_file = item
        environment = os.environ.copy()
        # SWE-bench exports contain exact builds from both defaults and
        # conda-forge. A host-level strict priority setting rejects those
        # otherwise valid mixed-channel locks.
        environment["CONDA_CHANNEL_PRIORITY"] = "flexible"
        process = _run(
            ["conda", "env", "create", "-f", environment_file],
            check=False,
            env=environment,
        )
        log_path = RUN_ROOT / "environment_logs" / (name + ".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "return_code={}\nstdout:\n{}\n\nstderr:\n{}".format(
                process.returncode, process.stdout, process.stderr
            ),
            encoding="utf-8",
        )
        return {
            "environment": name,
            "environment_file": environment_file,
            "log": str(log_path),
            "return_code": process.returncode,
        }

    results = []
    with ThreadPoolExecutor(
        max_workers=min(max(1, args.workers), len(tasks) or 1)
    ) as executor:
        futures = [executor.submit(create_one, task) for task in tasks]
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            print(
                "Environment progress: {}/{} {} rc={}".format(
                    index,
                    len(tasks),
                    result["environment"],
                    result["return_code"],
                ),
                flush=True,
            )
    output = {
        "created_or_attempted": sorted(
            results, key=lambda item: item["environment"]
        ),
        "preexisting_count": len(existing),
        "requested_new_count": len(tasks),
    }
    _write_json(RUN_ROOT / "environment_creation.json", output)
    failures = [r for r in results if r["return_code"] != 0]
    return 1 if failures else 0


def child_command(stage, record, args):
    environment = NAMERTS_ENVIRONMENTS[record["python_version"]]
    command = [
        "conda",
        "run",
        "--no-capture-output",
        "-n",
        environment,
        "python",
        "-m",
        "src.final_python_samples_validation",
        stage,
        "--instance",
        record["instance_id"],
        "--subset",
        str(Path(args.subset).resolve()),
        "--environment-manifest",
        str(Path(args.environment_manifest).resolve()),
        "--run-root",
        str(Path(args.run_root).resolve()),
        "--timeout",
        str(args.timeout),
    ]
    if stage in ("ground-truth", "namerts"):
        command.extend(["--workers", str(args.workers)])
    if stage == "ground-truth":
        command.extend(
            ["--output", str(ground_truth_path(record["repo"]).resolve())]
        )
    elif stage == "namerts":
        command.extend(
            [
                "--ground-truth",
                str(ground_truth_path(record["repo"]).resolve()),
            ]
        )
        if args.reuse_parent_cache:
            command.append("--reuse-parent-cache")
    return command


def command_batch_stage(args):
    records = load_subset(args.subset)
    by_project = defaultdict(list)
    for record in records:
        by_project[record["repo"]].append(record)

    def run_project(item):
        project, project_records = item
        project_results = []
        for record in project_records:
            command = child_command(args.stage, record, args)
            process = _run(command, cwd=PROJECT_ROOT, check=False)
            log_path = (
                Path(args.run_root)
                / PROJECTS[project]["slug"]
                / "batch_logs"
                / "{}_{}.log".format(args.stage, record["instance_id"])
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "command={}\nreturn_code={}\nstdout:\n{}\n\nstderr:\n{}".format(
                    " ".join(command),
                    process.returncode,
                    process.stdout,
                    process.stderr,
                ),
                encoding="utf-8",
            )
            project_results.append(
                {
                    "instance_id": record["instance_id"],
                    "log": str(log_path),
                    "return_code": process.returncode,
                }
            )
            if process.returncode != 0 and not args.continue_on_error:
                break
        return project, project_results

    results = []
    items = sorted(by_project.items())
    with ThreadPoolExecutor(
        max_workers=min(max(1, args.project_workers), len(items) or 1)
    ) as executor:
        futures = [executor.submit(run_project, item) for item in items]
        for index, future in enumerate(as_completed(futures), start=1):
            project, project_results = future.result()
            results.extend(project_results)
            print(
                "{} project progress: {}/{} {}".format(
                    args.stage, index, len(items), project
                ),
                flush=True,
            )
    _write_json(
        Path(args.run_root) / ("batch_{}_status.json".format(args.stage)),
        sorted(results, key=lambda item: item["instance_id"]),
    )
    failures = [result for result in results if result["return_code"] != 0]
    return 1 if failures else 0


def command_normalize_ground_truth(args):
    changed_records = 0
    for project in sorted(PROJECTS):
        path = ground_truth_path(project)
        if not path.exists():
            continue
        records = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for record in records:
            all_hits = sorted(
                set(
                    record.get(
                        "dynamic_hit_tests_all",
                        record.get("dynamic_hit_tests", []),
                    )
                )
            )
            passing_hits = sorted(
                set(
                    record.get(
                        "passing_dynamic_hit_tests",
                        record.get("dynamic_hit_tests", []),
                    )
                )
            )
            if (
                record.get("dynamic_hit_tests") != all_hits
                or record.get("passing_dynamic_hit_tests") != passing_hits
                or record.get("tests_to_run") != all_hits
            ):
                record["dynamic_hit_tests"] = all_hits
                record["passing_dynamic_hit_tests"] = passing_hits
                record["tests_to_run"] = all_hits
                changed = True
                changed_records += 1
        if changed:
            _write_json(path, records)
    print(
        "Normalized {} ground-truth records to retain failed-test hits".format(
            changed_records
        )
    )
    return 0


def command_verify_environments(args):
    manifest = json.loads(
        Path(args.environment_manifest).read_text(encoding="utf-8")
    )
    tasks = []
    for name, record in sorted(manifest["environments"].items()):
        tasks.append(
            {
                "environment": name,
                "expected_python": record["python_version"],
                "kind": "target",
            }
        )
    for version, name in sorted(NAMERTS_ENVIRONMENTS.items()):
        tasks.append(
            {
                "environment": name,
                "expected_python": version,
                "kind": "namerts",
            }
        )

    def verify_one(task):
        if task["kind"] == "namerts":
            code = (
                "import sys; "
                "from src.namebdp import NameBDP; "
                "from src.parser import TreeSitterClient; "
                "tree = TreeSitterClient.parse(b'def f():\\n    return 1\\n'); "
                "assert tree is not None; "
                "print('{}.{}.{}'.format(*sys.version_info[:3]))"
            )
        else:
            code = (
                "import sys; "
                "print('{}.{}.{}'.format(*sys.version_info[:3]))"
            )
        process = _run(
            [
                "conda",
                "run",
                "--no-capture-output",
                "-n",
                task["environment"],
                "python",
                "-c",
                code,
            ],
            cwd=PROJECT_ROOT,
            check=False,
        )
        actual = process.stdout.strip().splitlines()
        actual = actual[-1] if actual else ""
        result = dict(task)
        result.update(
            {
                "actual_python": actual,
                "return_code": process.returncode,
                "stderr": process.stderr.strip(),
                "safe": (
                    process.returncode == 0
                    and (
                        actual == task["expected_python"]
                        or actual.startswith(task["expected_python"] + ".")
                    )
                ),
            }
        )
        return result

    results = []
    with ThreadPoolExecutor(
        max_workers=min(max(1, args.workers), len(tasks) or 1)
    ) as executor:
        futures = [executor.submit(verify_one, task) for task in tasks]
        for future in as_completed(futures):
            results.append(future.result())
    output = {
        "schema_version": 1,
        "environment_count": len(results),
        "safe_count": sum(item["safe"] for item in results),
        "results": sorted(
            results,
            key=lambda item: (item["kind"], item["environment"]),
        ),
    }
    _write_json(args.output, output)
    print(
        "Verified {}/{} environments -> {}".format(
            output["safe_count"], len(results), Path(args.output).resolve()
        )
    )
    return 0 if output["safe_count"] == len(results) else 1


def command_classify_missing(args):
    records = load_subset(args.subset)
    ground_truth = {}
    for project in PROJECTS:
        path = ground_truth_path(project)
        if not path.exists():
            continue
        for item in json.loads(path.read_text(encoding="utf-8")):
            ground_truth[item["instance_id"]] = item
    results = []
    for record in records:
        slug = PROJECTS[record["repo"]]["slug"]
        comparison_paths = list(
            (Path(args.run_root) / slug / "comparisons").glob(
                "namerts_{}_*.json".format(record["instance_id"])
            )
        )
        if not comparison_paths:
            continue
        latest = max(
            comparison_paths,
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        comparison = json.loads(latest.read_text(encoding="utf-8"))
        expected = set(
            ground_truth.get(record["instance_id"], {}).get(
                "tests_to_run", comparison.get("ground_truth", [])
            )
        )
        selected = set(comparison.get("tests_to_run", []))
        missing = sorted(expected - selected)
        if not missing:
            continue
        classification = MISSING_CLASSIFICATIONS.get(
            record["instance_id"],
            {
                "category": "unreviewed",
                "version_related": None,
                "reason": "No manual classification has been recorded.",
            },
        )
        item = {
            "comparison": str(latest),
            "instance_id": record["instance_id"],
            "missing_tests": missing,
            "project": record["repo"],
            "python_version": record["python_version"],
        }
        item.update(classification)
        results.append(item)
    output = {
        "schema_version": 1,
        "instances_with_missing": len(results),
        "missing_test_files": sum(
            len(item["missing_tests"]) for item in results
        ),
        "version_related_instances": sum(
            item["version_related"] is True for item in results
        ),
        "classifications": sorted(
            results, key=lambda item: item["instance_id"]
        ),
    }
    _write_json(args.output, output)
    print(json.dumps(
        {
            "instances_with_missing": output["instances_with_missing"],
            "missing_test_files": output["missing_test_files"],
            "version_related_instances": output[
                "version_related_instances"
            ],
        },
        indent=2,
        sort_keys=True,
    ))
    return 0


def command_summary(args):
    records = load_subset(args.subset)
    smoke = {}
    ground_truth = {}
    comparisons = {}
    for record in records:
        slug = PROJECTS[record["repo"]]["slug"]
        smoke_path = (
            Path(args.run_root)
            / slug
            / "smoke"
            / (record["instance_id"] + ".json")
        )
        if smoke_path.exists():
            smoke[record["instance_id"]] = json.loads(
                smoke_path.read_text(encoding="utf-8")
            )
        gt_path = ground_truth_path(record["repo"])
        if gt_path.exists():
            for item in json.loads(gt_path.read_text(encoding="utf-8")):
                ground_truth[item["instance_id"]] = item
        comparison_paths = list(
            (Path(args.run_root) / slug / "comparisons").glob(
                "namerts_{}_*.json".format(record["instance_id"])
            )
        )
        if comparison_paths:
            latest = max(
                comparison_paths,
                key=lambda path: (path.stat().st_mtime, path.name),
            )
            comparisons[record["instance_id"]] = json.loads(
                latest.read_text(encoding="utf-8")
            )
    instances = []
    for record in records:
        instance_id = record["instance_id"]
        gt = ground_truth.get(instance_id, {})
        comparison = comparisons.get(instance_id, {})
        dynamic_hit_tests = sorted(
            set(gt.get("dynamic_hit_tests", []))
        )
        reviewed_ground_truth = sorted(
            set(gt.get("tests_to_run", dynamic_hit_tests))
        )
        selected = sorted(set(comparison.get("tests_to_run", [])))
        missing = sorted(set(reviewed_ground_truth) - set(selected))
        raw_missing = sorted(set(dynamic_hit_tests) - set(selected))
        instances.append(
            {
                "instance_id": instance_id,
                "project": record["repo"],
                "project_version": record["version"],
                "python_version": record["python_version"],
                "smoke_safe": smoke.get(instance_id, {}).get("safe"),
                "test_files": smoke.get(instance_id, {}).get(
                    "total_test_files"
                ),
                "passed_test_files": gt.get("passed_test_files"),
                "failed_test_files": len(gt.get("failed_test_files", [])),
                "timed_out_test_files": len(
                    gt.get("timed_out_test_files", [])
                ),
                "dynamic_hits": len(dynamic_hit_tests),
                "ground_truth": len(reviewed_ground_truth),
                "selected": len(selected),
                "missing": missing,
                "raw_missing": raw_missing,
            }
        )
    summary = {
        "schema_version": 1,
        "instances": instances,
        "aggregate": {
            "expected_instances": len(records),
            "smoke_completed": len(smoke),
            "smoke_safe": sum(
                item["smoke_safe"] is True for item in instances
            ),
            "ground_truth_completed": len(ground_truth),
            "comparisons_completed": len(comparisons),
            "instances_with_missing_tests": sum(
                bool(item["missing"]) for item in instances
            ),
            "dynamic_hit_test_files": sum(
                item["dynamic_hits"] for item in instances
            ),
            "ground_truth_test_files": sum(
                item["ground_truth"] for item in instances
            ),
            "selected_test_files": sum(
                item["selected"] for item in instances
            ),
            "missing_test_files": sum(
                len(item["missing"]) for item in instances
            ),
            "test_file_runs": sum(
                item["test_files"] or 0 for item in instances
            ),
            "passed_test_file_runs": sum(
                item["passed_test_files"] or 0 for item in instances
            ),
            "failed_test_file_runs": sum(
                item["failed_test_files"] for item in instances
            ),
            "timed_out_test_file_runs": sum(
                item["timed_out_test_files"] for item in instances
            ),
        },
    }
    _write_json(args.output, summary)
    print(json.dumps(summary["aggregate"], indent=2, sort_keys=True))
    return 0


def add_execution_arguments(parser):
    parser.add_argument("--instance", required=True)
    parser.add_argument("--subset", default=str(DEFAULT_SUBSET))
    parser.add_argument(
        "--environment-manifest", default=str(ENV_MANIFEST)
    )
    parser.add_argument("--run-root", default=str(RUN_ROOT))
    parser.add_argument("--timeout", type=int, default=900)


def build_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    prepare.add_argument("--output", default=str(DEFAULT_SUBSET))
    prepare.add_argument("--exclude-python", action="append", default=["3.5"])
    prepare.set_defaults(func=command_prepare)

    envs = subparsers.add_parser("generate-target-envs")
    envs.add_argument("--subset", default=str(DEFAULT_SUBSET))
    envs.add_argument("--output-dir", default=str(ENV_ROOT))
    envs.set_defaults(func=command_generate_target_envs)

    worktrees = subparsers.add_parser("create-worktrees")
    worktrees.add_argument("--subset", default=str(DEFAULT_SUBSET))
    worktrees.set_defaults(func=command_create_worktrees)

    create_envs = subparsers.add_parser("create-environments")
    create_envs.add_argument(
        "--environment-manifest", default=str(ENV_MANIFEST)
    )
    create_envs.add_argument("--workers", type=int, default=2)
    create_envs.set_defaults(func=command_create_environments)

    verify_envs = subparsers.add_parser("verify-environments")
    verify_envs.add_argument(
        "--environment-manifest", default=str(ENV_MANIFEST)
    )
    verify_envs.add_argument("--workers", type=int, default=6)
    verify_envs.add_argument(
        "--output", default=str(ENVIRONMENT_VERIFICATION_PATH)
    )
    verify_envs.set_defaults(func=command_verify_environments)

    normalize_gt = subparsers.add_parser("normalize-ground-truth")
    normalize_gt.set_defaults(func=command_normalize_ground_truth)

    smoke = subparsers.add_parser("smoke")
    add_execution_arguments(smoke)
    smoke.set_defaults(func=command_smoke)

    ground_truth = subparsers.add_parser("ground-truth")
    add_execution_arguments(ground_truth)
    ground_truth.add_argument("--workers", type=int, default=24)
    ground_truth.add_argument("--output", required=True)
    ground_truth.set_defaults(func=command_ground_truth)

    namerts = subparsers.add_parser("namerts")
    add_execution_arguments(namerts)
    namerts.add_argument("--workers", type=int, default=24)
    namerts.add_argument("--ground-truth", required=True)
    namerts.add_argument("--reuse-parent-cache", action="store_true")
    namerts.set_defaults(func=command_namerts)

    batch = subparsers.add_parser("batch-stage")
    batch.add_argument(
        "stage", choices=["smoke", "ground-truth", "namerts"]
    )
    batch.add_argument("--subset", default=str(DEFAULT_SUBSET))
    batch.add_argument(
        "--environment-manifest", default=str(ENV_MANIFEST)
    )
    batch.add_argument("--run-root", default=str(RUN_ROOT))
    batch.add_argument("--workers", type=int, default=24)
    batch.add_argument("--project-workers", type=int, default=4)
    batch.add_argument("--timeout", type=int, default=900)
    batch.add_argument("--continue-on-error", action="store_true")
    batch.add_argument("--reuse-parent-cache", action="store_true")
    batch.set_defaults(func=command_batch_stage)

    classify = subparsers.add_parser("classify-missing")
    classify.add_argument("--subset", default=str(DEFAULT_SUBSET))
    classify.add_argument("--run-root", default=str(RUN_ROOT))
    classify.add_argument("--output", default=str(MISSING_REVIEW_PATH))
    classify.set_defaults(func=command_classify_missing)

    summary = subparsers.add_parser("summarize")
    summary.add_argument("--subset", default=str(DEFAULT_SUBSET))
    summary.add_argument("--run-root", default=str(RUN_ROOT))
    summary.add_argument("--output", default=str(SUMMARY_PATH))
    summary.set_defaults(func=command_summary)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
