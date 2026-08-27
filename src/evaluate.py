import argparse
import json
import os
import tarfile
import time
from datetime import datetime
from pathlib import Path

import src.config as config
from src.framework import EkstaziP
from src.namebdp import NameBDP
from src.runall import Runall
from src.utils import collect_all_heuristic, seconds_to_timestr
from src.instance_logger import InstanceLogger
from src.utils import subprocess_run_stdout


def evaluate_instance(direct_parent, true_parent, current, target, tool_name, repo_path, conda_env, tool_class,
                      cached_files, time_tag, n, registry_decorator_keywords=None, use_isolation: bool = False,
                      selected_tests: set = None, collect_all_tests: bool = False,
                      run_parent: bool = True, patch_path: str = None,
                      run_current_tests: bool = True, reuse_parent_cache: bool = True):
    if registry_decorator_keywords is not None:
        config.REGISTRY_DECORATOR_KEYWORDS.clear()
        config.REGISTRY_DECORATOR_KEYWORDS.update(registry_decorator_keywords)

    def clear_cache():
        if not cached_files:
            return
        for cached_file in cached_files:
            abs_cached_file = os.path.join(repo_path, cached_file)
            if os.path.exists(abs_cached_file):
                Path(abs_cached_file).unlink()

    def store_cache(commit_hash):
        if not cached_files:
            return
        with tarfile.open(os.path.join(config.CACHE_PATH, f"{tool_name}_{commit_hash}.tar.gz"), "w:gz") as tar:
            for cached_file in cached_files:
                abs_cached_file = os.path.join(repo_path, cached_file)
                if not os.path.exists(abs_cached_file):
                    raise RuntimeError(f"Cache file {abs_cached_file} does not exist [{commit_hash}]")
                filename = os.path.basename(abs_cached_file)
                tar.add(abs_cached_file, arcname=filename)

    def load_cache(commit_hash):
        if not cached_files:
            return True
        cache_path = os.path.join(config.CACHE_PATH, f"{tool_name}_{commit_hash}.tar.gz")
        if not os.path.exists(cache_path):
            return False

        print(f"extracting from {cache_path}")
        with tarfile.open(cache_path, "r:gz") as tar:
            tar.extractall(path=repo_path)
        subprocess_run_stdout([f"cd {repo_path} && ls"], no_output=False)
        return True

    def run_tool(commit_hash, init: bool = False, patch_to_apply: str = None,
                 execute_tests: bool = True):
        start_time = time.time()
        reset_code = subprocess_run_stdout([f"cd {repo_path} && git reset --hard {commit_hash}"])
        if reset_code != 0:
            raise RuntimeError("Failed to reset {} to {}".format(repo_path, commit_hash))
        if patch_to_apply is not None:
            check_code = subprocess_run_stdout(
                [f"cd {repo_path} && git apply --check {patch_to_apply}"]
            )
            if check_code != 0:
                raise RuntimeError("Patch does not apply cleanly: {}".format(patch_to_apply))
            apply_code = subprocess_run_stdout(
                [f"cd {repo_path} && git apply {patch_to_apply}"]
            )
            if apply_code != 0:
                raise RuntimeError("Failed to apply patch: {}".format(patch_to_apply))
        current_selected_tests = (
            collect_all_heuristic(repo_path)
            if collect_all_tests
            else selected_tests
        )
        if current_selected_tests is not None:
            tool_obj = tool_class(
                project_path=repo_path,
                conda_env_name=conda_env,
                n=n,
                use_isolation=use_isolation,
                selected_tests=current_selected_tests,
            )
        else:
            tool_obj = tool_class(
                project_path=repo_path,
                conda_env_name=conda_env,
                n=n,
                use_isolation=use_isolation
            )
        _tests_to_run = tool_obj.get_tests_to_run(init=init)
        if execute_tests:
            tool_obj.run_and_update(_tests_to_run)
            store_cache(commit_hash)
        end_time = time.time()
        _elapsed_time = end_time - start_time
        _test_time = tool_obj.get_test_time()
        _select_time = _elapsed_time - _test_time
        return _tests_to_run, _test_time, _select_time

    InstanceLogger(
        logger_id=f"{tool_name}_{true_parent}_{current}",
        logger_dir=f"{tool_name}_{repo_path.replace('/', '')}_{time_tag}"
    )

    # no cache found
    clear_cache()
    init_time = 0.0
    parent_cache_loaded = run_parent and reuse_parent_cache and load_cache(true_parent)
    if run_parent and not parent_cache_loaded:
        _, init_test_time, init_select_time = run_tool(true_parent, init=True)
        init_time = init_test_time + init_select_time

    current_ref = current if current is not None else true_parent
    tests_to_run, test_time, select_time = run_tool(
        current_ref,
        patch_to_apply=patch_path,
        execute_tests=run_current_tests,
    )
    InstanceLogger().get_logger().info(f"[{true_parent}->{current}] tests to run: {len(tests_to_run)}")
    return {
        "current": current,
        "direct_parent": direct_parent,
        "true_parent": true_parent,
        "tests_to_run": list(tests_to_run),
        "init_time": seconds_to_timestr(init_time),
        "test_time": seconds_to_timestr(test_time),
        "select_time": seconds_to_timestr(select_time),
    }


SMOKE_COMMIT_COUNT = 3


def evaluate_tool(tool_name, commits_path, repo_path, conda_env, tool_class, cached_files, n,
                  registry_decorator_keywords=None, use_isolation: bool = False,
                  selected_tests_file: str = None, commit_limit: int = None,
                  collect_all_tests: bool = False, run_parent: bool = True):
    ret = []
    time_tag = str(time.time())
    os.makedirs(config.RESULTS, exist_ok=True)
    install_instrumentor(conda_env)

    with open(commits_path, "r") as commits_file:
        commit_pairs = [pair for pair in json.load(commits_file) if pair[3]]
    if commit_limit is not None:
        commit_pairs = commit_pairs[:commit_limit]
        print(f"Smoke test: evaluating {len(commit_pairs)} commit pairs from {commits_path}")

    selected_tests = dict()
    if selected_tests_file:
        with open(selected_tests_file, "r") as selected_file:
            selected = json.load(selected_file)
            for item in selected:
                selected_tests[item["current"]] = item["tests_to_run"]
    total_commit_pairs = len(commit_pairs)
    project_name = os.path.basename(os.path.normpath(repo_path))
    for index, (direct_parent, true_parent, current, target) in enumerate(commit_pairs, start=1):
        print(
            f"[{tool_name}][{project_name}] Progress {index}/{total_commit_pairs}: "
            f"{true_parent[:8]} -> {current[:8]}",
            flush=True,
        )
        _ret = evaluate_instance(direct_parent, true_parent, current, target, tool_name, repo_path, conda_env, tool_class,
                          cached_files, time_tag, n, registry_decorator_keywords, use_isolation,
                          selected_tests[current] if selected_tests_file else None, collect_all_tests,
                          run_parent)
        ret.append(_ret)

    mode_suffix = "_smoke" if commit_limit is not None else ""
    with open(os.path.join(config.RESULTS, f"{tool_name}_{conda_env}{mode_suffix}_{str(time.time())}.json"), "w") as json_file:
        json.dump(ret, json_file, indent=4)

CACHED_FILES_EKSTAZIP = [
        "dependencies.json",
        "py_checksums_cache.json"
]
def evaluate_ekstazip(commits_path, repo_path, conda_env, n, registry_decorator_keywords=None, commit_limit=None):
    evaluate_tool(
        tool_name="EkstaziP",
        commits_path=commits_path,
        repo_path=repo_path,
        conda_env=conda_env,
        tool_class = EkstaziP,
        cached_files=CACHED_FILES_EKSTAZIP,
        n=n,
        registry_decorator_keywords=registry_decorator_keywords,
        use_isolation=False,
        commit_limit=commit_limit,
    )

CACHED_FILES_NBDP = [
        "dependencies.json",
        "py_checksums_cache.json",
        "nbdp_cache.json",
        "critical_names.json"
]
def evaluate_nbdp(commits_path, repo_path, conda_env, n, registry_decorator_keywords=None, commit_limit=None):
    evaluate_tool(
        tool_name="NameBDP",
        commits_path=commits_path,
        repo_path=repo_path,
        conda_env=conda_env,
        tool_class=NameBDP,
        cached_files=CACHED_FILES_NBDP,
        n=n,
        registry_decorator_keywords=registry_decorator_keywords,
        use_isolation=False,
        commit_limit=commit_limit,
    )


def install_instrumentor(conda_env_name):
    # subprocess_run_stdout([f"conda run -n {conda_env_name} pip uninstall -y nbdp_instrumentor"])
    subprocess_run_stdout([f"cd {os.path.dirname(__file__)}/instrumentor && conda run -n {conda_env_name} pip install ."])


def clear_cache(dir_path=config.CACHE_PATH):
    dir_path = Path(dir_path)

    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
        return

    if not dir_path.is_dir():
        raise f"{dir_path} is not a directory"

    if next(dir_path.iterdir(), None) is None:
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = dir_path.with_name(f"{dir_path.name}_{timestamp}")

    dir_path.rename(backup_dir)
    dir_path.mkdir()
    print(f"Rotated {dir_path} -> {backup_dir}")


# for rq2
def evaluate_runall(commits_path, repo_path, conda_env, n, selected_tests_file, commit_limit=None):
    evaluate_tool(
        tool_name="Runall",
        commits_path=commits_path,
        repo_path=repo_path,
        conda_env=conda_env,
        tool_class = Runall,
        cached_files=[],
        n=n,
        selected_tests_file=selected_tests_file,
        commit_limit=commit_limit,
        run_parent=False,
    )


def evaluate_all_tests(commits_path, repo_path, conda_env, n, commit_limit=None):
    evaluate_tool(
        tool_name="Runall",
        commits_path=commits_path,
        repo_path=repo_path,
        conda_env=conda_env,
        tool_class=Runall,
        cached_files=[],
        n=n,
        use_isolation=False,
        commit_limit=commit_limit,
        collect_all_tests=True,
        run_parent=False,
    )


def selected_repos(seaborn: bool = False, repo: str = None):
    if repo is not None:
        if seaborn and repo != "seaborn":
            raise ValueError("--seaborn cannot be combined with a non-seaborn --repo")
        return [repo]
    if not seaborn:
        return ["sympy", "sklearn", "mpl", "dask", "xarray", "sphinx", "pylint", "pvlib", "loguru"]
    return ["seaborn"]


def run_rq2_test_only(seaborn: bool = False, repo: str = None, smoke: bool = False):
    repos = selected_repos(seaborn, repo)
    commit_limit = SMOKE_COMMIT_COUNT if smoke else None
    for repo in repos:
        commit_pairs = os.path.join(os.path.dirname(__file__), f"../dataset/{config.DATASET_FILE_NAME[repo]}")
        target_repo = config.TARGET_REPO_PATH[repo]
        env_name = config.ENV_NAME[repo]
        multiproc_n = config.ISOLATION_MULTIPROC_N[repo]
        selected_tests_file = config.RESULTS_PATH[repo]
        evaluate_runall(commit_pairs, target_repo, env_name, multiproc_n, selected_tests_file, commit_limit)


def run_all_tests(seaborn: bool = False, repo: str = None, smoke: bool = False):
    repos = selected_repos(seaborn, repo)
    commit_limit = SMOKE_COMMIT_COUNT if smoke else None
    for repo in repos:
        commit_pairs = os.path.join(os.path.dirname(__file__), f"../dataset/{config.DATASET_FILE_NAME[repo]}")
        target_repo = config.TARGET_REPO_PATH[repo]
        env_name = config.ENV_NAME[repo]
        multiproc_n = config.ISOLATION_MULTIPROC_N[repo]
        evaluate_all_tests(commit_pairs, target_repo, env_name, multiproc_n, commit_limit)


def test_all(seaborn: bool = False, prune_cf: bool = True, prune_nem: bool = True,
             para_n: int = 500, approach: str = "namerts", repo: str = None,
             smoke: bool = False):
    config.PRUNE_CRITICAL_FUNCTIONS = prune_cf
    config.PRUNE_NAME_RESOLUTION = prune_nem
    config.NUM_DYNAMIC_MONITOR = para_n
    clear_cache(config.CACHE_PATH)

    if approach == "namerts":
        evaluate_func = evaluate_nbdp
    elif approach == "ekstap":
        evaluate_func = evaluate_ekstazip
    else:
        raise NotImplementedError

    repos = selected_repos(seaborn, repo)
    commit_limit = SMOKE_COMMIT_COUNT if smoke else None
    for repo in repos:
        commit_pairs = os.path.join(os.path.dirname(__file__), f"../dataset/{config.DATASET_FILE_NAME[repo]}")
        target_repo = config.TARGET_REPO_PATH[repo]
        env_name = config.ENV_NAME[repo]
        multiproc_n = config.ISOLATION_MULTIPROC_N[repo]
        registry_decorator_keywords = config.REGISTRY_DECORATOR_KEYWORDS_REPO[repo]
        evaluate_func(
            commit_pairs,
            target_repo,
            env_name,
            multiproc_n,
            registry_decorator_keywords,
            commit_limit,
        )


def str2bool(v):
    if isinstance(v, bool):
        return v
    v = v.lower()
    if v in ("true", "1", "yes", "y"):
        return True
    if v in ("false", "0", "no", "n"):
        return False
    raise argparse.ArgumentTypeError("Boolean value expected.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run RTS evaluation on all repositories",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--approach",
        choices=["namerts", "ekstap"],
        default="namerts",
        help="RTS approach to evaluate",
    )

    parser.add_argument(
        "--prune-cf",
        type=str2bool,
        default=True,
        metavar="{true,false}",
        help="Enable critical function pruning",
    )

    parser.add_argument(
        "--prune-nem",
        type=str2bool,
        default=True,
        metavar="{true,false}",
        help="Enable name resolution pruning",
    )

    parser.add_argument(
        "--para-n",
        type=int,
        default=500,
        help="Number of dynamic monitors",
    )

    execution_mode = parser.add_mutually_exclusive_group()

    execution_mode.add_argument(
        "--testonly",
        action="store_true",
        help="Only run selected tests",
    )

    execution_mode.add_argument(
        "--runall",
        action="store_true",
        help="Run all tests for every evaluated commit",
    )

    parser.add_argument(
        "--seaborn",
        action="store_true",
        help="Run on the seaborn repo",
    )

    parser.add_argument(
        "--repo",
        choices=sorted(config.TARGET_REPO_PATH),
        help="Run only the selected repository",
    )

    parser.add_argument(
        "--smoke",
        action="store_true",
        help=f"Run only the first {SMOKE_COMMIT_COUNT} valid commit pairs",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.runall:
        run_all_tests(args.seaborn, args.repo, args.smoke)
    elif args.testonly:
        run_rq2_test_only(args.seaborn, args.repo, args.smoke)
    else:
        test_all(
            seaborn=args.seaborn,
            prune_cf=args.prune_cf,
            prune_nem=args.prune_nem,
            para_n=args.para_n,
            approach=args.approach,
            repo=args.repo,
            smoke=args.smoke,
        )
