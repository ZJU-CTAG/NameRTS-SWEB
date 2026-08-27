import difflib
import glob
import json
import os
import pprint
import re
import signal
import subprocess
import sys
import time
import traceback
import shlex
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import chain
from pathlib import Path
from types import CodeType
from typing import Iterable, List
from datetime import timedelta

import networkx as nx
import pandas as pd
from src.instance_logger import InstanceLogger


class Timer:
    def __init__(self, name: str = None):
        self.start_time = None
        self.end_time = None
        self.name = name

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.time()
        if self.name is not None:
            print(f"[{self.name}] taken: {self.time_string()}")
        else:
            print(f"Taken: {self.time_string()}")

    def elapsed_time(self):
        return self.end_time - self.start_time

    def time_string(self):
        return time.strftime("%H:%M:%S", time.gmtime(self.elapsed_time()))


def subprocess_run(cmd: list, cwd: str = None, timeout: int = None):
    cmd_str = ' '.join(cmd)
    InstanceLogger().get_logger().info(f"Running {cmd_str}.")

    process = subprocess.Popen(
        cmd_str,
        shell=True,
        executable="/bin/bash",
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )

    try:
        if timeout is not None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            stdout, stderr = process.communicate()
        return_code = process.returncode
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return_code = -1

    InstanceLogger().get_logger().info(f"Subprocess finished with return code: {return_code}, \n\nstdout:\n{stdout} \n\nstderr:\n{stderr}")
    return return_code, stdout, stderr


def subprocess_run_stdout(cmd: list, cwd: str = None, no_output: bool = False):
    cmd = ' '.join(cmd)
    InstanceLogger().get_logger().info(f"Running {cmd}.")
    process = subprocess.Popen(
        cmd,
        shell=True,
        executable="/bin/bash",
        cwd=cwd,
        stdout=subprocess.DEVNULL if no_output else sys.stdout,
        stderr=subprocess.DEVNULL if no_output else sys.stderr
    )

    return_code = process.wait()
    InstanceLogger().get_logger().info(f"Subprocess finished with return code: {return_code}")
    return return_code


def resolve_conda_python(conda_env_name: str) -> str:
    """Resolve an environment interpreter once, avoiding concurrent conda wrappers."""
    process = subprocess.Popen(
        ["conda", "run", "-n", conda_env_name, "python", "-c", "import sys; print(sys.executable)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            "Cannot resolve Python for conda env {}: {}".format(conda_env_name, stderr)
        )
    candidates = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not candidates or not os.path.exists(candidates[-1]):
        raise RuntimeError(
            "Conda env {} returned an invalid Python path: {!r}".format(
                conda_env_name, stdout
            )
        )
    return candidates[-1]


def run_test_files_parallel(
    project_path: str,
    conda_env_name: str,
    test_command: str,
    test_files,
    workers: int,
    timeout: int = None,
    test_targets=None,
    capture_dependencies: bool = False,
    nbdp_capturing: bool = False,
):
    """Run one test file per process with bounded outer-level concurrency."""
    test_files = sorted(test_files)
    if not test_files:
        return []
    env_python = resolve_conda_python(conda_env_name)
    env_bin = str(Path(env_python).parent)
    process_env = os.environ.copy()
    process_env["PATH"] = env_bin + os.pathsep + process_env.get("PATH", "")
    process_env["OMP_NUM_THREADS"] = "1"
    process_env["OPENBLAS_NUM_THREADS"] = "1"
    process_env["MKL_NUM_THREADS"] = "1"
    if capture_dependencies:
        bootstrap_dir = str(Path(__file__).parent / "runtime_bootstrap")
        process_env["NAMERTS_CAPTURE_DEPENDENCIES"] = "1"
        process_env["NAMERTS_PROJECT_ROOT"] = os.path.abspath(project_path)
        process_env["NAMERTS_COVERAGE_DIR"] = os.path.join(
            os.path.abspath(project_path), "coverage"
        )
        process_env["NAMERTS_NBDP_CAPTURING"] = (
            "1" if nbdp_capturing else "0"
        )
        old_pythonpath = process_env.get("PYTHONPATH", "")
        process_env["PYTHONPATH"] = (
            bootstrap_dir
            if not old_pythonpath
            else bootstrap_dir + os.pathsep + old_pythonpath
        )

    def run_one(test_file):
        target = (
            test_targets.get(test_file, test_file)
            if test_targets is not None
            else test_file
        )
        command = test_command + " " + shlex.quote(target)
        started = time.time()
        test_process_env = process_env.copy()
        if capture_dependencies:
            test_process_env["NAMERTS_TEST_FILE"] = test_file
        process = subprocess.Popen(
            ["/bin/bash", "-c", command],
            cwd=project_path,
            env=test_process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            universal_newlines=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            # The test runner is a child of the bash wrapper. Killing only
            # bash leaves a CPU-heavy orphan behind, so terminate the whole
            # per-file process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            return_code = -1
        return {
            "test_file": test_file,
            "return_code": return_code,
            "timed_out": timed_out,
            "duration": time.time() - started,
            "stdout": stdout,
            "stderr": stderr,
        }

    results = []
    pool_size = min(max(1, workers), len(test_files))
    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        futures = {executor.submit(run_one, test_file): test_file for test_file in test_files}
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if result["return_code"] != 0:
                InstanceLogger().get_logger().warning(
                    "Test file failed: {} (exit {}, timeout={})\nstdout:\n{}\nstderr:\n{}".format(
                        result["test_file"],
                        result["return_code"],
                        result["timed_out"],
                        result["stdout"],
                        result["stderr"],
                    )
                )
            if completed == len(test_files) or completed % 10 == 0:
                print(
                    "Test-file progress: {}/{}".format(completed, len(test_files)),
                    flush=True,
                )
    results.sort(key=lambda item: item["test_file"])
    failures = [item for item in results if item["return_code"] != 0]
    InstanceLogger().get_logger().info(
        "Per-file test run finished: {} total, {} nonzero".format(
            len(results), len(failures)
        )
    )
    return results


def collect_all(project_path: str, conda_env_name: str):
    """
    relative paths
    :param project_path:
    :param conda_env_name:
    :return:
    """
    subprocess_run_stdout([f"cd {project_path} && "
                           # f"conda activate {conda_env_name} && "
                           f"conda run -n {conda_env_name} pytest --collect-only --json-report --json-report-file=collect.json"],
                          no_output=True)

    collect_path = os.path.join(project_path, "collect.json")
    assert os.path.exists(collect_path), "collect.json does not exist"
    with open(collect_path, "r") as collect_file:
        collect = json.load(collect_file)

    ret = set()
    for result in list(dict(collect)["collectors"]):
        nodeid = dict(result).get("nodeid", "")
        if nodeid.endswith(".py"):
            ret.add(nodeid)

    InstanceLogger().get_logger().info(f"Collected test files: \n{pprint.pformat(ret)}\n\n")
    return ret


def co_is_attribute(co):
    return hasattr(co, "class_name") and co.class_name is not None


def is_test_file_pytest(p, project_path):
    if p is None:
        return False
        # normalize to Path
    p = Path(p) if not isinstance(p, Path) else p

    project_name = os.environ.get("NAMERTS_PROJECT_NAME") or os.path.basename(
        os.path.normpath(project_path)
    )
    is_django = project_name == "django"

    # Django uses many ``tests.py`` modules with its own unittest runner.
    valid_name = (
        p.name.startswith("test_")
        or p.name.startswith("unittest_")
        or (is_django and p.name == "tests.py")
    )
    if not valid_name:
        return False

    if is_django:
        try:
            rel_p = p.resolve().relative_to(Path(project_path).resolve())
        except ValueError:
            return False
        return len(rel_p.parts) > 1 and rel_p.parts[0] == "tests"

    # Historical Requests releases keep their pytest module directly at the
    # repository root (``test_requests.py``).
    if project_name == "requests":
        try:
            rel_p = p.resolve().relative_to(Path(project_path).resolve())
        except ValueError:
            return False
        if len(rel_p.parts) == 1:
            return True

    # such files seem not meant to be run by pytest
    if project_name == "prefect":
        rel_p = Path(p)
        # absolute path
        if rel_p.is_absolute():
            rel_p = rel_p.relative_to(Path(project_path).resolve())
        if len(rel_p.parts) != 0 and rel_p.parts[0] != "tests":
            return False
    elif project_name == "pandas":
        rel_p = Path(p)
        # absolute path
        if rel_p.is_absolute():
            rel_p = rel_p.relative_to(Path(project_path).resolve())
        if len(rel_p.parts) != 0 and rel_p.parts[0] != "pandas":
            return False

    # check all ancestors for a directory named "tests"
    return any(parent.name in ["tests", "testing"] for parent in p.parents)


def collect_all_heuristic(project_path: str):
    root = Path(project_path).resolve()
    return set(
        str(p.relative_to(root))
        for p in root.rglob('*.py')
        if is_test_file_pytest(p, project_path)
    )


def get_all_py_files(project_path: str):
    root = Path(project_path).resolve()
    return set(
        str(p.relative_to(root))
        for p in root.rglob('*.py')
    )


def pyc_to_py(pyc_path: str) -> str:
    s = pyc_path.replace("__pycache__/", "")
    s = re.sub(r'\.cpython-.*?\.pyc$', '.py', s)
    return s


EXCLUDE_PATHS = ["build"]
def get_all_pycs(project_path: str):

    return [
        str(path.relative_to(project_path)) for path in Path(project_path).rglob("*.pyc")
        if "-pytest-" not in path.name
           and not any(exclude_path in Path(path).parts for exclude_path in EXCLUDE_PATHS)
    ]


def compare_checksums_dict(previous: dict, current: dict):
    old_file_paths = set(previous.keys())
    new_file_paths = set(current.keys())

    added = new_file_paths - old_file_paths
    removed = old_file_paths - new_file_paths
    common = old_file_paths & new_file_paths
    changed = {
        key for key in common
        if previous[key] != current[key]
    }

    changed_file_paths = added | removed | changed
    return changed_file_paths, added, removed


def merge_dict_list(dicts):
    merged = defaultdict(list)
    for key, values in chain.from_iterable(d.items() for d in dicts):
        merged[key].extend(values)
    return dict(merged)


def merge_dict_set(dicts):
    merged = defaultdict(set)
    for key, values in chain.from_iterable(d.items() for d in dicts):
        merged[key].update(values)
    return dict(merged)


def merge_dict_dict_set(d1, d2):
    result = defaultdict(lambda: defaultdict(set))
    # 先处理 d1
    for k1, inner in d1.items():
        for k2, s in inner.items():
            result[k1][k2] |= s
    # 再处理 d2
    for k1, inner in d2.items():
        for k2, s in inner.items():
            result[k1][k2] |= s
    return {k1: dict(inner) for k1, inner in result.items()}


def compile_all(project_path: str, conda_env_name: str, no_output = True):
    subprocess_run_stdout([f'find {project_path} -name "*.pyc" -delete'],
                          no_output=no_output)
    if "pylint" in project_path:
        exit_code = subprocess_run_stdout([f"cd {project_path} && "
                                           f"conda run -n {conda_env_name} python -m compileall . -f -q -x '(^|/)(tests/regrtest_data|tests/input|tests/functional|doc|venv)(/|$)'"],
                                          no_output=False)
    elif "matplotlib" in project_path:
        exit_code = subprocess_run_stdout([f"cd {project_path} && "
                                           f"conda run -n {conda_env_name} python -m compileall . -f -q -x '(^|/)(build|subprojects)(/|$)' "],
                                          no_output=False)
    elif "transformers" in project_path:
        exit_code = subprocess_run_stdout([f"cd {project_path} && "
                                           f"conda run -n {conda_env_name} python -m compileall . -f -q -x '(^|/)(docs|examples|templates|docker|build)(/|$)'"],
                                          no_output=False)
    elif "pandas" in project_path:
        exit_code = subprocess_run_stdout([f"cd {project_path}/pandas && "
                                           f"conda run -n {conda_env_name} python -m compileall . -f -q"],
                                          no_output=False)
    elif "prefect" in project_path:
        exit_code = subprocess_run_stdout([f"cd {project_path} && "
                                           f"conda run -n {conda_env_name} python -m compileall . -f -q -x 'src/integrations'"],
                                          no_output=False)
    elif "loguru" in project_path:
        exit_code = subprocess_run_stdout([f"cd {project_path} && "
                                           f"conda run -n {conda_env_name} python -m compileall . -f -q -x 'tests/exceptions'"],
                                          no_output=False)
    elif "sphinx" in project_path:
        exit_code = subprocess_run_stdout([f"cd {project_path} && "
                                           f"conda run -n {conda_env_name} python -m compileall . -f -q -x 'tests/roots'"],
                                          no_output=False)
    else:
        exit_code =  subprocess_run_stdout([f"cd {project_path} && "
                               f"conda run -n {conda_env_name} python -m compileall . -f"],
                              no_output=no_output)
    return exit_code


def compute_coverage(graph, entries):
    g = nx.DiGraph()
    # A test module with no extracted imports is still a valid graph entry.
    # NetworkX doesn't create nodes for empty adjacency sets implicitly.
    g.add_nodes_from(entries)
    for node, neighbors in graph.items():
        g.add_node(node)
        for neighbor, _, _ in neighbors:
            g.add_edge(node, neighbor)

    coverage = {}
    for entry in entries:
        try:
            coverage[entry] = nx.descendants(g, entry) | {entry}  # 包含自身
        except Exception as e:
            InstanceLogger().get_logger().error(f"Failed to compute coverage for {entry}: {e}\n{traceback.format_exc()}\n\n")
    return coverage


def conftests_for_test_file(
    test_file: str,
    conftest_files: Iterable[str],
) -> List[str]:
    """
    返回给定测试文件会加载的全部 conftest.py（从项目根到就近目录的顺序）。
    路径均为相对路径字符串（相对于项目根目录）。
    假设：
      - conftest_files 都是相对路径形式的 "xxx/conftest.py"
      - test_file 也是相对路径形式的 "xxx/test_xxx.py"
      - 所有路径共享同一项目根
    """
    tf_dir = Path(test_file).parent
    cfiles = [Path(p) for p in conftest_files]

    def is_ancestor(ancestor: Path, descendant: Path) -> bool:
        try:
            descendant.relative_to(ancestor)
            return True
        except ValueError:
            return False

    # 只保留父目录是测试文件祖先目录的 conftest.py
    eligible = [cf for cf in cfiles if is_ancestor(cf.parent, tf_dir)]

    # 按父目录深度（parts 数）排序，从浅到深
    eligible.sort(key=lambda p: len(p.parent.parts))

    # 输出为原始相对路径字符串
    return [str(cf) for cf in eligible]


def get_all_str_consts(code: CodeType):
    strs = set()
    for c in code.co_consts:
        if isinstance(c, str):
            strs.add(c)
        elif isinstance(c, CodeType):
            strs.update(get_all_str_consts(c))
    return strs


# 1. 识别四种安全 import，一旦有逗号就返回 None
def parse_single_import_line(line: str):
    """
    尝试把一行代码解析成一个我们能安全替换的 import 语句。
    支持：
      - import xxx
      - import xxx as yyy
      - from xxx import yyy
      - from xxx import yyy as zzz
    且只处理“单个名字”，行里出现逗号就跳过。
    返回：
      {
        "kind": "import" | "from",
        "module": "os" / "os.path" / ...,
        "imported": "getenv",         # 仅 for kind == "from"
        "alias": "xxx",               # 可能为 None
        "local_name": "xxx",          # 这个 import 在当前作用域中应该出现的名字
      }
    或 None
    """
    stripped = line.strip()
    if "," in stripped:
        return None

    # import xxx [as yyy]
    m_imp = re.match(r'^import\s+([a-zA-Z_][\w\.]*)\s*(?:as\s+([a-zA-Z_]\w*))?\s*$', stripped)
    if m_imp:
        mod_name = m_imp.group(1)
        alias = m_imp.group(2)

        # “import pkg.sub” 且没有 as 的情况，不要动，直接返回 None
        if "." in mod_name and not alias:
            return None

        if alias:
            local_name = alias
        else:
            local_name = mod_name
        return {
            "kind": "import",
            "module": mod_name,
            "imported": None,
            "alias": alias,
            "local_name": local_name,
        }

    # from xxx import yyy [as zzz]
    m_from = re.match(r'^from\s+([a-zA-Z_][\w\.]*)\s+import\s+([a-zA-Z_]\w*)(?:\s+as\s+([a-zA-Z_]\w*))?\s*$', stripped)
    if m_from:
        mod_name = m_from.group(1)
        imported = m_from.group(2)
        alias = m_from.group(3)
        local_name = alias if alias else imported
        return {
            "kind": "from",
            "module": mod_name,
            "imported": imported,
            "alias": alias,
            "local_name": local_name,
        }

    return None


# 2. 根据解析结果生成真正要替换进去的代码块（行列表）
def make_cached_import_block(parsed: dict, cache_name: str, leading_ws: str, indent: str):
    """
    parsed: parse_single_import_line 的返回值
    """
    lines = []
    lines.append(f"{leading_ws}global {cache_name}\n")
    lines.append(f"{leading_ws}if {cache_name} is None:\n")

    if parsed["kind"] == "import":
        # import xxx [as yyy]
        mod_name = parsed["module"]
        lines.append(f"{leading_ws}{indent}import {mod_name} as _tmp\n")
        lines.append(f"{leading_ws}{indent}{cache_name} = _tmp\n")
        # 每次调用都要恢复出 local_name
        lines.append(f"{leading_ws}{parsed['local_name']} = {cache_name}\n")
    else:
        # from xxx import yyy [as zzz]
        mod_name = parsed["module"]
        imported = parsed["imported"]
        lines.append(f"{leading_ws}{indent}from {mod_name} import {imported} as _tmp\n")
        lines.append(f"{leading_ws}{indent}{cache_name} = _tmp\n")
        lines.append(f"{leading_ws}{parsed['local_name']} = {cache_name}\n")

    return lines


def find_root_package(project_root: str, file_path: str) -> str:
    """
    Determine the root Python package for a given file path.
    `project_root` must be an absolute path (string).
    `file_path` can be absolute or relative (string).
    """
    project_root = Path(project_root).resolve()
    p = Path(file_path)

    if p.is_absolute():
        try:
            relative_path = p.relative_to(project_root)
        except ValueError:
            raise ValueError(f"Absolute path {p} is not under project root {project_root}")
    else:
        relative_path = p

    full_path = project_root / relative_path
    curr = full_path.parent
    last_pkg = None

    while True:
        init_file = curr / "__init__.py"
        if init_file.exists():
            last_pkg = curr.name
        else:
            break
        if curr == project_root:
            break
        curr = curr.parent

    return last_pkg


def seconds_to_timestr(sec: float) -> str:
    td = timedelta(seconds=round(sec, 2))
    return str(td)


def timestr_to_seconds(s: str) -> float:
    td = pd.to_timedelta(s)
    return td.total_seconds()

