import argparse
import json
import os.path
import pprint
import shutil
import sys
import time
import traceback
from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from src.checksum import get_changed_files
from src.config import DIVIDE_DYNAMIC_IMPORTS, get_test_command, get_test_target, FAST_DEBUG
from src.import_collector import extract_imports
from src.utils import collect_all, collect_all_heuristic, compile_all, compute_coverage, is_test_file_pytest, get_all_py_files, conftests_for_test_file
from src.instance_logger import InstanceLogger
from src.parser import TreeSitterClient
from src.utils import subprocess_run_stdout, Timer, subprocess_run, run_test_files_parallel
import src.config as config


class EkstaziP:
    def __init__(self, project_path: str, conda_env_name: str, n: int = 60, divide_dynamic: bool = False, use_isolation: bool = False, nbdp_capturing: bool = False):
        self.test_time = 0.0
        self.project_path = project_path
        self.conda_env_name = conda_env_name
        self.coverage_file = Path(self.project_path) / "coverage.json"
        self.coverage_path = Path(self.project_path) / "coverage"
        self.dep_file = Path(self.project_path) / "dependencies.json"
        self.deps_dict_raw = dict()
        self.dyna_deps = dict()
        self.use_deps = dict()
        self.use_critical = dict()
        self.getattr_names = dict()
        self.member_names = dict()
        self.dependencies_dict = dict()
        self.removed = set()
        self.changed_files = set()
        self.all_test_paths = collect_all_heuristic(self.project_path)
        self.all_py_paths = get_all_py_files(self.project_path)
        self.init_run = False
        self.pool_size = n
        # Kept only for compatibility with existing callers. Test files are
        # always run in separate processes, so no xdist isolation is needed.
        self.use_isolation = False
        self.nbdp_capturing = nbdp_capturing
        self.file_contents = dict()
        self.parsed_trees = dict()
        self.using_wildcard_import = set()
        self.conftest_paths = {p for p in self.all_py_paths if os.path.basename(p) == "conftest.py"}
        InstanceLogger().get_logger().info(f"[EkstaziP] conftest_paths: \n{pprint.pformat(self.conftest_paths)}\n\n")

    def get_test_time(self):
        return self.test_time


    def get_file_content(self, file_path):
        """
        输入相对路径
        :param file_path:
        :return:
        """
        if file_path not in self.file_contents:
            with open(os.path.join(self.project_path, file_path), "r", encoding="utf-8", errors="ignore") as f:
                self.file_contents[file_path] = f.read()
        return self.file_contents[file_path]


    def get_parsed_tree(self, file_path):
        """
        输入相对路径
        :param file_path:
        :return:
        """
        if file_path not in self.parsed_trees:
            file_content = self.get_file_content(file_path)
            self.parsed_trees[file_path] = TreeSitterClient.parse(file_content.encode("utf-8"))
        return self.parsed_trees[file_path]


    def abs_to_rel(self, abs_path):
        abs_path = Path(abs_path)
        try:
            return str(abs_path.relative_to(self.project_path))
        except ValueError:
            return str(abs_path)


    def abs_to_rel_deps(self, abs_deps):
        return {(self.abs_to_rel(a), self.abs_to_rel(b), l) for a, b, l in abs_deps}


    def run_and_catch_dep(self, tests_to_run: list = None, added_lines: dict = None):
        if added_lines is None:
            added_lines = dict()
        if self.coverage_path.exists():
            shutil.rmtree(self.coverage_path)

        if not tests_to_run:
            return 0.0

        tests_to_run = sorted(tests_to_run)
        tests_to_run_file = "tests_to_run.txt"
        tests_to_run_path = os.path.join(self.project_path, tests_to_run_file)

        with open(tests_to_run_path, "w") as f:
            f.write("\n".join(tests_to_run))
        test_command = get_test_command(self.project_path)
        test_targets = {
            test_file: get_test_target(self.project_path, test_file)
            for test_file in tests_to_run
        }

        start_time = time.time()
        run_test_files_parallel(
            self.project_path,
            self.conda_env_name,
            test_command,
            tests_to_run,
            self.pool_size,
            timeout=config.PER_FILE_TEST_TIMEOUT,
            test_targets=test_targets,
            capture_dependencies=True,
            nbdp_capturing=self.nbdp_capturing,
        )
        end_time = time.time()
        self.test_time = end_time - start_time

        try:
            os.remove(tests_to_run_path)
        except:
            pass

        dynamic_deps = defaultdict(lambda: defaultdict(set))
        use_deps = defaultdict(set)
        use_critical = defaultdict(lambda: defaultdict(set))
        getattr_names = defaultdict(set)
        member_names = defaultdict(set)
        if self.coverage_path.exists():
            coverage_files = list(Path(self.coverage_path).glob("*.json.*"))
            for coverage_file in coverage_files:
                with open(coverage_file, "r") as f:
                    try:
                        # TODO: why parsing errors?
                        deps = set((a, b, l) for a, b, l in json.load(f))
                        importers = {a for a, _, _ in deps}
                        deps = {(a, b, l) for a, b, l in deps if b is None or b.startswith(self.project_path) or b in importers or
                                a in {"captured_getattr_name", "captured_member_name"}}
                    except Exception as e:
                        error_msg = f"Failed to parse coverage file: {coverage_file}: {e}\n{traceback.format_exc()}"
                        print(error_msg)
                        InstanceLogger().get_logger().error(error_msg)
                        continue


                coverage_file = os.path.basename(coverage_file)
                if coverage_file.startswith("import_pairs"):
                    global_scope_used_critical = {d for d in deps if d[0].startswith("critical_function")}
                    for dep in global_scope_used_critical:
                        identifier = dep[0][len("critical_function_"):]
                        identifier = identifier.replace("___dot___", ".").replace("___slash___", "/")
                        file_path, class_name, func_name = identifier.split("____", 2)
                        use_critical["*"][func_name].add((file_path, class_name))

                    deps = {d for d in deps if not d[0].startswith("decorator_checksum_") and not d[0].startswith("critical_function") and d[0] not in {"captured_getattr_name", "captured_member_name"}}
                    for dep in deps:
                        importer = self.abs_to_rel(dep[0])
                        imported = self.abs_to_rel(dep[1])
                        dynamic_deps["*"][importer].add((imported, None, None))
                else:
                    # test_file_rel = str(coverage_file).replace("..", "/").replace(".json", "")
                    test_file_rel = str(coverage_file).replace("..", "/")
                    suffix_idx = test_file_rel.find(".json")
                    if suffix_idx == -1:
                        print("Cannot parse name for coverage file:", coverage_file)
                        continue
                    else:
                        test_file_rel = test_file_rel[:suffix_idx]
                        for dep in deps:
                            if dep[0].startswith("decorator_checksum_"):
                                checksum = dep[0][len("decorator_checksum_"):]
                                use_deps[test_file_rel].add(checksum)
                            elif dep[0].startswith("critical_function_"):
                                identifier = dep[0][len("critical_function_"):]
                                identifier = identifier.replace("___dot___", ".").replace("___slash___", "/")
                                file_path, class_name, func_name = identifier.split("____", 2)
                                use_critical[test_file_rel][func_name].add((file_path, class_name))
                            elif dep[0] == "captured_getattr_name":
                                attr_name = dep[1]
                                getattr_names[test_file_rel].add(attr_name)
                            elif dep[0] == "captured_member_name":
                                member_name = dep[1]
                                member_names[test_file_rel].add(member_name)
                            else:
                                importer = self.abs_to_rel(dep[0])
                                imported = self.abs_to_rel(dep[1])
                                dynamic_deps[test_file_rel][importer].add((imported, None, None))

        # if self.coverage_path.exists():
        #     shutil.rmtree(self.coverage_path)

        # 更新记录
        for test, import_dict in dynamic_deps.items():
            orig_file_imported_dict = self.dyna_deps.get(test, dict())
            for importer, imported_set in import_dict.items():
                if importer in orig_file_imported_dict:
                    orig_file_imported_dict[importer].update(imported_set)
                else:
                    orig_file_imported_dict[importer] = imported_set
            self.dyna_deps[test] = orig_file_imported_dict
        for test, deps in use_deps.items():
            self.use_deps[test] = deps
        for test, used in use_critical.items():
            orig_func_used_dict = self.use_critical.get(test, dict())
            for func_name, item_set in used.items():
                if func_name in orig_func_used_dict:
                    orig_func_used_dict[func_name].update(item_set)
                else:
                    orig_func_used_dict[func_name] = item_set
            self.use_critical[test] = orig_func_used_dict
        for test, attr_names in getattr_names.items():
            if test in self.getattr_names:
                self.getattr_names[test].update(attr_names)
            else:
                self.getattr_names[test] = attr_names
        for test, _member_names in member_names.items():
            if test in self.member_names:
                self.member_names[test].update(_member_names)
            else:
                self.member_names[test] = _member_names


    def dump_dep_dict(self):
        dep_dict = {a: list(b) for a, b in self.deps_dict_raw.items()}
        use_deps = {a: list(b) for a, b in self.use_deps.items()}
        getattr_names = {a: list(b) for a, b in self.getattr_names.items()}
        member_names = {a: list(b) for a, b in self.member_names.items()}
        new_use_critical = dict()
        for test_file, _use_critical in self.use_critical.items():
            new_use_critical[test_file] = {a: list(b) for a, b in _use_critical.items()}
        dynamic_deps = dict()
        for test_file, _dyna_dps in self.dyna_deps.items():
            dynamic_deps[test_file] = {a: list(b) for a, b in _dyna_dps.items()}
        using_wildcard_import = list(self.using_wildcard_import)

        to_dump = {
            "dep_dict": dep_dict,
            "dynamic_deps": dynamic_deps,
            "use_deps": use_deps,
            "use_critical": new_use_critical,
            "getattr_names": getattr_names,
            "member_names": member_names,
            "using_wildcard_import": using_wildcard_import,
        }
        with open(self.dep_file, "w") as f:
            json.dump(to_dump, f, indent=4)


    def update_imports(self, file_path):
        self.using_wildcard_import.discard(file_path)
        file_content = self.get_file_content(file_path)
        parsed_tree = self.get_parsed_tree(file_path)
        imports = set(extract_imports(file_path, file_content, parsed_tree, self.project_path))
        imports_no_wildcards = {(path, name, alias) for path, name, alias in imports if name != "*"}
        if len(imports) != len(imports_no_wildcards):
            self.using_wildcard_import.add(file_path)
        self.deps_dict_raw[file_path] = imports_no_wildcards


    def get_tests_to_run(self, naive: bool = False, init: bool = False):
        # STEP1: find changed files
        with Timer("Compiling all .py files") as _:
            compile_all(self.project_path, self.conda_env_name)

        with Timer("Find changed files") as _:
            changed_files, added, removed = get_changed_files(self.project_path, naive=naive)
            self.changed_files = changed_files
            self.removed = removed
            InstanceLogger().get_logger().info(f"[EkstaziP] Changed files: \n{pprint.pformat(changed_files)}")

            # if any test files are modified, they should be executed
            changed_test_files = set([f for f in changed_files if is_test_file_pytest(f, self.project_path)])
            InstanceLogger().get_logger().info(f"[EkstaziP] Changed tests: \n{pprint.pformat(changed_test_files)}\n\n\n")

        # STEP2: find tests dependent on changed files
        # # TODO: handle cases where EkstaziP is not initialized
        # assert os.path.exists(self.dep_file), "Dependent file does not exist"

        if os.path.exists(self.dep_file):
            with Timer("Compute file deps") as _:
                with open(self.dep_file, "r") as f:
                    contents = dict(json.load(f))
                    self.use_deps = dict(contents["use_deps"])
                    self.using_wildcard_import = set(contents["using_wildcard_import"])

                    # 将 json 中读取的 list 转化为 set
                    self.member_names = {
                        file: set(inner)
                        for file, inner in dict(contents["member_names"]).items()
                    }
                    self.getattr_names = {
                        file: set(inner)
                        for file, inner in dict(contents["getattr_names"]).items()
                    }
                    self.deps_dict_raw = {
                        file: {tuple(item) for item in inner}
                        for file, inner in dict(contents["dep_dict"]).items()
                    }
                    self.dyna_deps = {
                        file: {k: {tuple(item) for item in v} for k, v in inner.items()}
                        for file, inner in dict(contents["dynamic_deps"]).items()
                    }
                    self.use_critical = {
                        file: {k: {tuple(item) for item in v} for k, v in inner.items()}
                        for file, inner in dict(contents["use_critical"]).items()
                    }
        else:
            InstanceLogger().get_logger().warning("[EkstaziP] No cached dependencies found. ")
            self.init_run = True
            tests_to_run = self.all_test_paths

        # 如果有文件被修改，重新 parse 并计算 imports
        for py_path in self.changed_files:
            if py_path in removed:
                continue
            self.update_imports(py_path)

        for test_file in self.all_test_paths:
            _conftest_paths = {(f, None, None) for f in conftests_for_test_file(test_file, self.conftest_paths)}
            if test_file in self.deps_dict_raw:
                self.deps_dict_raw[test_file].update(_conftest_paths)
            else:
                self.deps_dict_raw[test_file] = _conftest_paths

        deps_dict = deepcopy(self.deps_dict_raw)
        imported_no_source = set()
        for _, import_dict in self.dyna_deps.items():
            for importer, imported_set in import_dict.items():
                if importer.startswith("/") and "pytest" not in importer:
                    imported_no_source.update(imported_set)
                else:
                    if importer in deps_dict:
                        deps_dict[importer].update(imported_set)
                    else:
                        deps_dict[importer] = imported_set
        for test_file in self.all_test_paths:
            deps_dict.get(test_file, set()).update(imported_no_source)
        self.dependencies_dict = compute_coverage(deps_dict, self.all_test_paths)

        if not self.init_run:
            # 将新增文件加入所有依赖：因为还不确定哪些文件会使用他
            for dep_files in self.dependencies_dict.values():
                dep_files.update(added)

            InstanceLogger().get_logger().info(
                f"[EkstaziP] Dependencies Dict: \n{pprint.pformat(self.dependencies_dict)}\n\n\n")

            affected_tests = set(
                [key for key, values in self.dependencies_dict.items() if
                 any(item in changed_files for item in values)])
            InstanceLogger().get_logger().info(f"[EkstaziP] Affected tests: \n{pprint.pformat(affected_tests)}\n\n\n")

            tests_to_run = (changed_test_files | affected_tests) & self.all_test_paths

        InstanceLogger().get_logger().info(f"[EkstaziP] Tests to run: {len(tests_to_run)}/{len(self.all_test_paths)}: \n{pprint.pformat(tests_to_run)}\n\n")
        print(f"[EkstaziP] Tests to run: {len(tests_to_run)}/{len(self.all_test_paths)}")

        return tests_to_run

    def run_and_update(self, tests_to_run, added_lines = None):
        if added_lines is None:
            added_lines = dict()
        with Timer("Run tests and update deps") as _:
            # if self.init_run and not FAST_DEBUG:
            #     for test_file in tests_to_run:
            #         self.run_and_catch_dep([test_file], added_lines)
            if tests_to_run:
                self.run_and_catch_dep(list(tests_to_run), added_lines)

            # in case a file is deleted
            for removed_file in self.removed:
                self.deps_dict_raw.pop(removed_file, None)
                self.dyna_deps.pop(removed_file, None)
                self.use_deps.pop(removed_file, None)
                self.use_critical.pop(removed_file, None)
                self.getattr_names.pop(removed_file, None)
                self.member_names.pop(removed_file, None)
                self.using_wildcard_import.discard(removed_file)

            self.dump_dep_dict()


    def main(self):
        tests_to_run = self.get_tests_to_run(naive=True)

        self.run_and_update(tests_to_run)
