import difflib
import itertools
import json
import os
import pprint
from collections import defaultdict
from src.bytecode import parse_pyc, CodeObject, FunctionObject, ModuleObject, ClassObject, json_dump_default, \
    json_load_object_hook, VariableObject
from src.config import OPERATOR_OVERLOADING_ALL, INSTRUMENTATION_BLACKLIST, \
    SELECT_USE_PARALLEL
import src.config as config
from src.framework import EkstaziP
from src.utils import pyc_to_py, get_all_pycs, merge_dict_list, is_test_file_pytest, merge_dict_dict_set, \
    merge_dict_set, co_is_attribute, parse_single_import_line, make_cached_import_block
from src.instance_logger import InstanceLogger
from src.parser import TreeSitterClient
from src.utils import Timer
from detect_indent import detect_indent
from multiprocessing import Pool
import traceback
from typing import List, Tuple


_GLOBAL_SELF = None
_GLOBAL_DIFF_CHECKSUMS = None
_GLOBAL_INIT = None


def instrument_name_introspection_source(source):
    """Instrument getattr/getmembers expressions without wrapping the calls.

    The capture helper for getattr runs while evaluating its name argument and
    returns that value unchanged. The getmembers helper receives its completed
    result. Consequently neither helper is on the stack while the original
    introspection operation executes.
    """
    if "getattr" not in source and "getmembers" not in source:
        return source, 0

    source_bytes = source.encode("utf-8")
    tree = TreeSitterClient.parse(source_bytes)
    calls = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call":
            calls.append(node)
        stack.extend(reversed(node.children))

    getmembers_calls = []
    getattr_arguments = []
    for call in calls:
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        function_name = None
        if function.type == "identifier":
            function_name = source_bytes[
                function.start_byte:function.end_byte
            ].decode("utf-8")
        elif function.type == "attribute":
            attribute = function.child_by_field_name("attribute")
            if attribute is not None:
                function_name = source_bytes[
                    attribute.start_byte:attribute.end_byte
                ].decode("utf-8")

        if function_name == "getmembers":
            getmembers_calls.append(call)
        elif function_name == "getattr":
            named_arguments = list(arguments.named_children)
            if len(named_arguments) < 2:
                continue
            name_argument = named_arguments[1]
            if name_argument.type == "keyword_argument":
                name_argument = name_argument.child_by_field_name("value")
            if name_argument is not None:
                getattr_arguments.append(name_argument)

    # Keep only outer getmembers calls so replacements never overlap. The
    # completed outer result contains the member names relevant to the caller.
    getmembers_calls = [
        call
        for call in getmembers_calls
        if not any(
            outer is not call
            and outer.start_byte <= call.start_byte
            and call.end_byte <= outer.end_byte
            for outer in getmembers_calls
        )
    ]

    # Avoid overlapping edits when a getattr call is nested inside a
    # getmembers call. Capturing the completed getmembers result is sufficient
    # for that expression.
    getattr_arguments = [
        argument
        for argument in getattr_arguments
        if not any(
            call.start_byte <= argument.start_byte
            and argument.end_byte <= call.end_byte
            for call in getmembers_calls
        )
    ]

    replacements = []
    helper_prefix = b"__import__('nbdp_instrumentor')."
    for call in getmembers_calls:
        original = source_bytes[call.start_byte:call.end_byte]
        replacement = (
            helper_prefix
            + b"capture_getmembers_result("
            + original
            + b")"
        )
        replacements.append((call.start_byte, call.end_byte, replacement))
    for argument in getattr_arguments:
        original = source_bytes[argument.start_byte:argument.end_byte]
        replacement = (
            helper_prefix
            + b"capture_getattr_name("
            + original
            + b")"
        )
        replacements.append(
            (argument.start_byte, argument.end_byte, replacement)
        )

    for start, end, replacement in sorted(replacements, reverse=True):
        source_bytes = source_bytes[:start] + replacement + source_bytes[end:]
    return source_bytes.decode("utf-8"), len(replacements)


def _init_pool(self_ref, different_checksums, init):
    global _GLOBAL_SELF, _GLOBAL_DIFF_CHECKSUMS, _GLOBAL_INIT
    _GLOBAL_SELF = self_ref
    _GLOBAL_DIFF_CHECKSUMS = different_checksums
    _GLOBAL_INIT = init


def _run_algorithm(e):
    self_ref = _GLOBAL_SELF
    module_object = self_ref.world_pyc_dict[e]
    dependent_checksums, found_definitions, log_output, iter_count = \
        self_ref.algorithm(e, module_object, _GLOBAL_DIFF_CHECKSUMS, _GLOBAL_INIT)
    return e, dependent_checksums, found_definitions, log_output, iter_count


def _defaultdict_set():
    return defaultdict(set)


def _defaultdict_dict():
    return defaultdict(dict)


class NameBDP:

    def __init__(self, project_path: str, conda_env_name: str, n: int = 60, use_isolation: bool = False):
        self.project_path = project_path
        self.conda_env_name = conda_env_name
        self.world_name_dict = None
        self.world_pyc_dict = dict()
        self.module_name_dict_all = dict()
        self.defined_names = defaultdict(set)
        self.iter_counts = []
        self.init_mapping = defaultdict(list)
        self.defined_classes = dict()
        self.subclasses = defaultdict(set)
        self.valid_names = set()
        self.decorator_hash_2_elements = defaultdict(list)
        self.registered_classes = set()
        self.ekstazip = EkstaziP(
            project_path=project_path,
            conda_env_name=conda_env_name,
            n=n,
            use_isolation=use_isolation,
            nbdp_capturing=True
        )
        self.non_attr_import_graph = defaultdict(_defaultdict_set)
        self.import_alias = defaultdict(dict)
        self.attr_class_constrain_dict = defaultdict(dict)
        self.non_attr_file_constrain_dict = defaultdict(dict)
        self.used_alias_mapping = defaultdict(dict)
        self.found_definitions = defaultdict(int)
        self.critical_names_path = os.path.join(self.project_path, "critical_names.json")
        self.critical_names = list()
        self.modified_by_external_modules = defaultdict(_defaultdict_set)
        self.added_functions = defaultdict(set)
        self.deleted_functions_in_sc = defaultdict(set)
        self.ins_blacklist = INSTRUMENTATION_BLACKLIST.get(os.path.basename(os.path.normpath(self.project_path)), set())
        if config.PRUNE_CRITICAL_FUNCTIONS and os.path.exists(self.critical_names_path):
            with open(self.critical_names_path, "r") as f:
                self.critical_names = json.load(f)

    def get_test_time(self):
        return self.ekstazip.get_test_time()

    def algorithm(self, test_file: str, entry_object: ModuleObject, different_checksums, init: bool = False):
        log_output = ""
        found_definitions = defaultdict(int)
        world_dict = self.refine_world_dict(test_file)
        use_critical_test_sure = self.ekstazip.use_critical.get(test_file, dict())

        # 全局区使用的 critical function，可能被任何测试使用
        global_scope_used_critical = self.ekstazip.use_critical.get("*", dict())
        use_critical_test = merge_dict_set([use_critical_test_sure, global_scope_used_critical, self.added_functions, self.deleted_functions_in_sc])
        # log_output += f"use_critical_test for {test_file}: \n{pprint.pformat(use_critical_test, indent=4)}\n\n"

        def get_elements(target_name: str, target_name_type, source: str) -> Tuple[List[CodeObject], bool]:
            """

            :param source: where the target is constrained to be
            :param target_name:
            :param target_name_type: 0 - non-attribute, 1 - attribute, 2 - can be both
            :return:
            """
            nonlocal relevant_items
            corr_cos = world_dict.get(target_name, [])

            if not config.PRUNE_NAME_RESOLUTION:
                pass
            elif target_name_type == 0:
                corr_cos = [co for co in corr_cos if not co_is_attribute(co)]
                if source is not None and source != "*":
                    corr_cos = [co for co in corr_cos if co.file_path == source]
            elif target_name_type == 1:
                corr_cos = [co for co in corr_cos if co_is_attribute(co)]
                # TODO: maybe need fallback if no definition is found?
                corr_cos = [co for co in corr_cos if source is None or co.class_name == source]

            if target_name in self.critical_names and not init:
                source_constrain = use_critical_test.get(target_name, set())
                corr_cos = [co for co in corr_cos if not isinstance(co, FunctionObject) or (co.file_path, "None" if co.class_name is None else co.class_name) in source_constrain or os.path.basename(co.file_path) in self.ins_blacklist]

            ret = []
            # class_handled = False
            for co in corr_cos:
                # OOP: attributes need a corresponding class
                if co_is_attribute(co) and co.class_name not in relevant_names:
                    continue

                # # for classes: find init method
                # if not class_handled and isinstance(co, ClassObject):
                #     ret.extend(self.init_mapping.get(co.name, []))
                #     class_handled = True

                ret.append(co)

            return ret, len(corr_cos) == len(ret)

        log_output += f"Handling {entry_object.file_path}\n\n"

        work_stack = [(name, 0, source) for name in entry_object.external_names for source in self.non_attr_file_constrain_dict.get(test_file, dict()).get(name, {None})] + \
                     [(both, 2, None) for both in entry_object.external_boths]

        # 加入全局调用的副作用函数
        init_names_from_importing = []
        if not init:
            related_files = self.ekstazip.dependencies_dict.get(test_file, set())
        else:
            related_files = self.ekstazip.all_py_paths
        for related_file in related_files:
            if is_test_file_pytest(related_file, ""):
                continue
            imported_module_obj = self.world_pyc_dict.get(related_file, None)
            if imported_module_obj is not None:
                init_names_from_importing.extend([(name, 0, source) for name in imported_module_obj.external_names for source in self.non_attr_file_constrain_dict.get(imported_module_obj.file_path, dict()).get(name, {None})])
                init_names_from_importing.extend([(both, 2, None) for both in imported_module_obj.external_boths])
        # log_output += f"[init_names_from_importing]\n{pprint.pformat(init_names_from_importing, indent=4)}\n\n"
        work_stack.extend(init_names_from_importing)

        # inject fixtures
        injected_fixtures = []
        for fixture_name in entry_object.injected_fixtures:
            corr_co_set = world_dict.get(fixture_name, [])
            for co in corr_co_set:
                if isinstance(co, FunctionObject) and not co.class_name and co.file_path.endswith("conftest.py"):
                    injected_fixtures.append((fixture_name, 0, co.file_path))
        # log_output += f"Injected fixtures: \n{pprint.pformat(injected_fixtures, indent=4)}\n\n"
        work_stack.extend(injected_fixtures)

        # 被注册的类无法捕获其动态使用，因此默认被使用
        work_stack += list((name, 0, "*") for name in self.registered_classes)

        # init attrs
        getattr_names = set(self.ekstazip.getattr_names.get(test_file, set())) | set(self.ekstazip.getattr_names.get("*", set()))
        getattr_names = getattr_names & set(world_dict.keys())
        work_stack += list((attr_name, 2, None) for attr_name in getattr_names)

        # init names
        member_names = set(self.ekstazip.member_names.get(test_file, set())) | set(self.ekstazip.member_names.get("*", set()))
        member_names = member_names & set(world_dict.keys())
        self.ekstazip.member_names[test_file] = member_names
        work_stack += list((member_name, 2, None) for member_name in member_names)

        # operator overloading
        work_stack += [(oo_name, 2, None) for oo_name in self.get_all_operator_overloading_names()]

        # # 将确认会执行的函数加入
        # for name, source_set in use_critical_test_sure.items():
        #     for file_path, class_name in source_set:
        #         if class_name == "None":
        #             work_stack.append((name, 0, file_path))
        #         else:
        #             work_stack.append((class_name, 0, file_path))
        #             work_stack.append((name, 1, class_name))

        # ok_names 不需要再次 propagate: 这些 name 对应的所有实现都已经处理过了
        ok_names = set()

        relevant_items = set(work_stack)
        # work_stack = list(relevant_items)
        relevant_names = {i[0] for i in relevant_items}
        prev_name_count = len(relevant_items)
        iter_count = 0
        dependent_elements = set()

        def handle_element(code_object: CodeObject, name, n_type, source, i_new_items):
            nonlocal log_output
            if hasattr(code_object, "checksum"):
                checksum = code_object.checksum
                if checksum in dependent_elements and not isinstance(code_object, VariableObject):
                    return
                dependent_elements.add(checksum)

            file_path = code_object.file_path
            class_name = getattr(code_object, "class_name", "dummy_class_name_not_even_exist_hahahahaha")
            used_items = ({(name, 0, source) for name in code_object.external_names for source in self.non_attr_file_constrain_dict.get(file_path, dict()).get(name, set())} |
                         {(attr, 1, source) for attr in code_object.external_attributes for source in self.attr_class_constrain_dict.get(class_name, dict()).get(attr, set())} |
                         {(both, 2, None) for both in code_object.external_boths})

            if isinstance(code_object, VariableObject):
                modified_by_modules = self.modified_by_external_modules[file_path][code_object.name]
                if modified_by_modules:
                    used_items |= {(code_object.name, 0, source) for source in modified_by_modules}
            new_items = used_items - relevant_items
            if not self.critical_names and isinstance(code_object, FunctionObject):
                found_definitions[name] += len(used_items)
                # found_definitions[name] += len(new_items)

            # if new_items:
            #     log_output += f"New names:\n({name}, {n_type}, {source})\n -- propagate --> \n{json.dumps(list(new_items))}\n\n"

            relevant_items.update(new_items)
            relevant_names.update({i[0] for i in new_items})
            i_new_items.update(new_items)
            for n in new_items:
                work_stack.append(n)

        used_decorators = self.ekstazip.use_deps.get(test_file, [])
        for decorator_hash in used_decorators:
            elements_list = self.decorator_hash_2_elements.get(decorator_hash, [])
            for element in elements_list:
                handle_element(element, element.name, -1, None, set())

        # log_output += f"Init relevant names: \n{pprint.pformat(relevant_items)}\n\n"

        while True:
            if not init and dependent_elements & different_checksums:
                return dependent_elements, found_definitions, log_output, iter_count
            iter_count += 1

            iter_new_items = set()
            while work_stack:
                current_name, name_type, source = work_stack.pop()

                corr_elements, get_all = get_elements(current_name, name_type, source)
                if get_all:
                    ok_names.add((current_name, name_type, source))
                for corr_co in corr_elements:
                    handle_element(corr_co, current_name, name_type, source, iter_new_items)

            # log_output += f"New names for iter {iter_count}: \n{pprint.pformat(iter_new_items)}\n\n"

            # stop iterating when:
            # 1. relevant_items stop converging, or
            # 2. no new class names are founded
            if len(relevant_items) == prev_name_count or len({i[0] for i in iter_new_items} & self.defined_classes.keys()) == 0:
                break
            prev_name_count = len(relevant_items)
            work_stack = [item for item in (relevant_items - ok_names)]

        log_output += f"Name-based dependency propagation for {entry_object.file_path} end in {iter_count} iterations. \n\n"
        return dependent_elements, found_definitions, log_output, iter_count


    def build_init_mapping(self):
        init_names = ["__init__", "__new__", "__call__", "__init_subclass__"]
        for init_name in init_names:
            for init_obj in self.world_name_dict.get(init_name, []):
                if init_obj.class_name is not None:
                    self.init_mapping[init_obj.class_name].append(init_obj)


    def build_names_set(self):
        # assume that a name can not be both a class and a function
        # TODO: is this true?
        approximated_checksum_counts = 0
        # with open("/tmp.json", "w") as f:
        #     json.dump(self.world_name_dict, f, default=json_dump_default, indent=4)

        for name, objs in self.world_name_dict.items():
            for obj in objs:
                if isinstance(obj, FunctionObject):
                    approximated_checksum_counts += len(objs)
                    self.valid_names.add(name)
                elif isinstance(obj, ClassObject):
                    for super_class_name in obj.super_classes:
                        self.subclasses[super_class_name].add(name)
                    if obj.name in self.defined_classes:
                        self.defined_classes[obj.name].add(obj)
                    else:
                        self.defined_classes[obj.name] = {obj}
                    self.valid_names.add(name)
                    self.valid_names.update(set(obj.local_variables.keys()))
                elif isinstance(obj, ModuleObject):
                    self.valid_names.update(set(obj.local_variables.keys()))
        self.valid_names.update(set().union(*(alias_dict.keys() for alias_dict in self.import_alias.values())))
        InstanceLogger().get_logger().info(f"valid_names: \n{pprint.pformat(self.valid_names)}")
        InstanceLogger().get_logger().info(f"subclasses: \n{pprint.pformat(self.subclasses)}")
        return approximated_checksum_counts


    def build_d_hash_mapping(self):
        for name, objs in self.world_name_dict.items():
            for obj in objs:
                if isinstance(obj, FunctionObject) and obj.decorator_hash:
                    self.decorator_hash_2_elements[obj.decorator_hash].append(obj)


    def build_registered_classes(self):
        for name, objs in self.world_name_dict.items():
            for obj in objs:
                if isinstance(obj, ClassObject) and obj.is_registered:
                    self.registered_classes.add(obj.name)

                    # 注册式装饰器有时似乎也会读取子类并进行注册。有没有更优雅的处理方法？
                    obj.external_boths.update(self.subclasses.get(obj.name, set()))


    def get_all_operator_overloading_names(self):
        return {name for name in self.valid_names if name in OPERATOR_OVERLOADING_ALL}


    def add_global_variables(self):
        all_objs = []
        for obj_list in self.world_name_dict.values():
            for obj in obj_list:
                all_objs.append(obj)

        # 在 moduleA 全局区修改 moduleB 的全局变量
        for module_obj in self.world_name_dict.get("<module>", []):
            file_path = module_obj.file_path
            for variable_name in module_obj.local_variables:
                source_paths = self.non_attr_import_graph.get(file_path, dict()).get(variable_name, None)
                if source_paths is not None:
                    for source_path in source_paths:
                        self.modified_by_external_modules[source_path][variable_name].add(file_path)
        InstanceLogger().get_logger().info(f"[modified_by_external_modules]\n{pprint.pformat(self.modified_by_external_modules, indent=4)}\n\n")

        for obj in all_objs:
            if isinstance(obj, (ModuleObject, ClassObject)):
                local_variables = obj.local_variables
                if isinstance(obj, ClassObject):
                    class_name = obj.name
                else:
                    class_name = None
                for name, item in local_variables.items():
                    new_variable = VariableObject(name, obj.file_path, item["external_names"], item["external_attributes"], item["external_boths"], item["checksum"], class_name)
                    if name in self.world_name_dict:
                        self.world_name_dict[name].append(new_variable)
                    else:
                        self.world_name_dict[name] = [new_variable]


    def remove_invalid_names(self):
        # 对于 both，他可能是某个 module 中使用的 alias，但是我们不知道是哪个 module，因此需要全部加入
        package_aliases_dict = dict()
        for importer, alias_dict in self.import_alias.items():
            if not "__init__.py" in importer:
                continue
            for alias, orig in alias_dict.items():
                package_aliases_dict[alias] = orig
        InstanceLogger().get_logger().info(f"[package_aliases_dict]\n{pprint.pformat(package_aliases_dict, indent=4)}\n\n")

        for objs in self.world_name_dict.values():
            for obj in objs:
                package_aliases = {b[1:] for b in obj.external_boths if b.startswith("?")}
                obj.external_boths.update(package_aliases)
                added_package_unaliases = {package_aliases_dict[a] for a in package_aliases if a in package_aliases_dict}

                # TODO: can we handle more package aliases? Now only those of root package have been handled
                if added_package_unaliases:
                    InstanceLogger().get_logger().info(f"Added package unaliases for {obj}: {pprint.pformat(added_package_unaliases, indent=4)}\n\n")
                    obj.external_boths.update(added_package_unaliases)

                obj.external_boths |= {self.import_alias[obj.file_path][k] for k in obj.external_boths if k in self.import_alias.get(obj.file_path, dict())}
                obj.external_names = obj.external_names & self.valid_names
                obj.external_attributes = obj.external_attributes & self.valid_names
                obj.external_boths = obj.external_boths & self.valid_names


    def resolve_external_boths(self):
        for objs in self.world_name_dict.values():
            for obj in objs:
                obj.external_names = obj.external_names | obj.external_boths
                obj.external_attributes = obj.external_attributes | obj.external_boths


    def refine_world_dict(self, test_file_rel):
        # Dependency extraction can legitimately yield an isolated test
        # module. Treat it as depending on itself instead of assuming every
        # entry has at least one graph edge.
        related_files = self.ekstazip.dependencies_dict.get(
            test_file_rel, {test_file_rel}
        )

        refined_world_dict = dict()
        for obj_name, obj_list in self.world_name_dict.items():
            new_obj_list = [obj for obj in obj_list if obj.file_path in related_files or obj.file_path.endswith("conftest.py")]
            refined_world_dict[obj_name] = new_obj_list
        return refined_world_dict


    def instrument_monitored_functions(self):
        instrument_dict = defaultdict(list)

        for element_list in self.world_name_dict.values():
            for element in element_list:
                if not (isinstance(element, FunctionObject) and element.decorator_hash):
                    continue
                instrument_dict[element.file_path].append((element.body_starts_line, element.decorator_hash, "decorator_checksum"))

        for critical_name in self.critical_names:
            for element in self.world_name_dict.get(critical_name, []):
                if not isinstance(element, FunctionObject):
                    continue
                file_path = element.file_path
                class_name = element.class_name if element.class_name else "None"
                identifier = f"{file_path.replace('/', '___slash___').replace('.', '___dot___')}____{class_name}____{critical_name}"
                instrument_dict[file_path].append((element.body_starts_line, identifier, "critical_function"))


        added_lines = defaultdict(lambda: defaultdict(int))
        file_backups = dict()
        for file_path, instrument_list in instrument_dict.items():
            if os.path.basename(file_path) in self.ins_blacklist:
                continue

            file_path_abs = os.path.join(self.project_path, file_path)
            with open(file_path_abs, "r") as file:
                file_contents = file.read()
            indent = detect_indent(file_contents)['indent']
            if indent == "":
                indent = " " * 4
            file_backups[file_path] = file_contents

            file_lines = file_contents.splitlines(keepends=True)
            parsed_tree = TreeSitterClient().parse(file_contents.encode("utf-8"))
            line_2_entry = TreeSitterClient().get_line_to_func_body_mapping(parsed_tree.root_node)

            init_code = "import nbdp_instrumentor\n"
            to_be_initialized_2 = {(identifier, monitor_type) for _, identifier, monitor_type in instrument_list}
            for identifier, monitor_type in to_be_initialized_2:
                init_code += f"nbdp_instrumentor.{monitor_type}_{identifier} = False\n"
                init_code += f"nbdp_instrumentor.import_{monitor_type}_{identifier} = False\n"

            setup_loc = next(
                (len(file_lines) - 1 - i for i, s in enumerate(reversed(file_lines)) if
                 s.startswith("from __future__ import")),
                -1
            )
            if setup_loc == -1:
                file_lines[0] = init_code + file_lines[0]
                added_lines[file_path][0+1] += 1 + 2 * len(to_be_initialized_2)
            else:
                file_lines[setup_loc] += init_code
                added_lines[file_path][setup_loc + 1] += 1 + 2 * len(to_be_initialized_2)

            for line, identifier, monitor_type in instrument_list:
                # 这些行号是从 1 开始的
                if line in line_2_entry:
                    line = line_2_entry[line]
                else:
                    InstanceLogger().get_logger().error(f"Body start line not found for {identifier} in {file_path}:{line}")
                    continue
                s = file_lines[line - 1]
                while not s.strip():
                    line += 1
                    s = file_lines[line - 1]
                leading_ws = s[:len(s) - len(s.lstrip())]
                file_lines[line - 1] = (
                            leading_ws + "import nbdp_instrumentor\n" +
                            leading_ws + f"if nbdp_instrumentor.is_importing: \n" +
                            leading_ws + indent + f"if not nbdp_instrumentor.import_{monitor_type}_{identifier}: \n" +
                            leading_ws + indent + indent + f"nbdp_instrumentor.import_{monitor_type}_{identifier} = True\n" +
                            leading_ws + indent + indent + f"nbdp_instrumentor.visited_when_importing.add(\"{monitor_type}_{identifier}\")\n" +
                            leading_ws + f"elif not nbdp_instrumentor.{monitor_type}_{identifier}: \n" +
                            leading_ws + indent + f"nbdp_instrumentor.{monitor_type}_{identifier} = True\n" +
                            s)
                added_lines[file_path][line] += 7

            new_file_contents = "".join(file_lines)

            diff = difflib.unified_diff(
                file_contents.splitlines(keepends=True),
                new_file_contents.splitlines(keepends=True),
                fromfile='before.py',
                tofile='after.py'
            )
            diff_text = ''.join(diff)
            InstanceLogger().get_logger().info(f"Instrumented {file_path}: \n{diff_text}")

            with open(file_path_abs, "w") as file:
                file.write(new_file_contents)

        introspection_count = 0
        introspection_files = 0
        for file_path in sorted(self.ekstazip.all_py_paths):
            file_path_abs = os.path.join(self.project_path, file_path)
            try:
                with open(
                    file_path_abs, "r", encoding="utf-8", errors="strict"
                ) as source_file:
                    file_contents = source_file.read()
                instrumented, count = instrument_name_introspection_source(
                    file_contents
                )
            except (OSError, UnicodeError):
                continue
            if not count:
                continue
            if file_path not in file_backups:
                file_backups[file_path] = file_contents
            with open(file_path_abs, "w", encoding="utf-8") as source_file:
                source_file.write(instrumented)
            introspection_count += count
            introspection_files += 1
        InstanceLogger().get_logger().info(
            "Instrumented {} getattr/getmembers expressions in {} files".format(
                introspection_count, introspection_files
            )
        )

        # added_lines 现在里面的行都是 instrument 前的行。需要更新为修改后的行
        new_added_lines = dict()
        for file_path, _added_lines in added_lines.items():
            _new_added_lines = defaultdict(int)
            for _added_line, lines in _added_lines.items():
                fixed_added_line = _added_line + sum(a for l, a in _added_lines.items() if l < _added_line)
                _new_added_lines[fixed_added_line] += lines
            new_added_lines[file_path] = _new_added_lines

        return file_backups, new_added_lines


    def file_recover(self, file_backups):
        for file_path, file_contents in file_backups.items():
            file_path_abs = os.path.join(self.project_path, file_path)
            with open(file_path_abs, "w") as file:
                file.write(file_contents)


    def make_non_attr_import_graph(self):
        for importer, imported_set in self.ekstazip.deps_dict_raw.items():
            for imported, name, alias in imported_set:
                if imported and name:
                    self.non_attr_import_graph[importer][name].add(imported)
                    if alias:
                        self.import_alias[importer][alias] = name
        InstanceLogger().get_logger().info(f"[non_attr_import_graph]\n{pprint.pformat(self.non_attr_import_graph)}\n\n")
        InstanceLogger().get_logger().info(f"[import_alias]\n{pprint.pformat(self.import_alias)}\n\n")


    def build_defined_names(self):
        for name, obj_list in self.world_name_dict.items():
            for obj in obj_list:
                if not co_is_attribute(obj):
                    self.defined_names[obj.file_path].add(name)
                else:
                    self.defined_names[obj.class_name].add(name)
        InstanceLogger().get_logger().info(f"[defined_names]\n{pprint.pformat(self.defined_names)}\n\n")


    def build_constrain_dicts(self):
        all_super_classes = dict()
        all_sub_classes = dict()
        def get_super_sub_classes(_class_name, get_type, dep=0):
            if dep >= 100:
                return set()
            if get_type == "super" and _class_name in all_super_classes:
                return set(all_super_classes[_class_name])
            if get_type == "sub" and _class_name in all_sub_classes:
                return set(all_sub_classes[_class_name])

            super_sub_classes = set()
            if _class_name not in self.defined_classes:
                return super_sub_classes
            if get_type == "super":
                # current_super_sub_classes = self.defined_classes[_class_name].super_classes
                current_super_sub_classes = set().union(*[class_obj.super_classes for class_obj in self.defined_classes[_class_name]])
            else:
                current_super_sub_classes = self.subclasses.get(_class_name, set())
            super_sub_classes.update(current_super_sub_classes)
            for super_sub_class in current_super_sub_classes:
                super_sub_classes.update(get_super_sub_classes(super_sub_class, get_type, dep=dep + 1))

            if get_type == "super":
                all_super_classes[_class_name] = super_sub_classes
            elif get_type == "sub":
                all_sub_classes[_class_name] = super_sub_classes
            return super_sub_classes

        for co_list in self.world_name_dict.values():
            for code_object in co_list:
                file_path = code_object.file_path
                class_name = getattr(code_object, "class_name", None)

                # 给定一个 non_attr，以及其所被使用的文件，确定其可能的所有定义所在的文件
                # 理论上来讲，有且仅有一个

                external_names_copy = code_object.external_names.copy()
                for name in external_names_copy:
                    if name not in self.non_attr_file_constrain_dict[file_path]:
                        def add_name_constraint(_current_file, target_name, visited_set, dep=0):
                            visited_set.add(_current_file)

                            # 第一种情况：找到 alias
                            _alias = self.used_alias_mapping[_current_file].get(target_name, None)
                            constrain_dict = self.non_attr_file_constrain_dict[_current_file]
                            if _alias and _alias in constrain_dict:
                                return constrain_dict[_alias], _alias

                            # 第二种情况：没有找到 alias
                            constrain_dict = self.non_attr_file_constrain_dict[_current_file]
                            if target_name in constrain_dict:
                                return constrain_dict[target_name], None

                            candidate_file_paths = set()
                            target_name_alias = self.import_alias.get(_current_file, dict()).get(target_name, None)

                            # 第一种情况：当前文件中有定义
                            if target_name in self.defined_names.get(_current_file, set()):
                                candidate_file_paths.add(_current_file)
                            # 第二种情况：如果当前文件被修改: fallback （import 不可用）
                            elif _current_file in self.ekstazip.changed_files:
                                candidate_file_paths.add("*")
                            # 第三种情况：当前文件中有 import
                            elif target_name_alias is not None and len(self.non_attr_import_graph[_current_file][target_name_alias]) != 0:
                                import_files = self.non_attr_import_graph[_current_file][target_name_alias]
                                for import_file in import_files:
                                    if import_file not in visited_set:
                                        source_set, used_alias = add_name_constraint(import_file, target_name_alias if target_name_alias else target_name, visited_set.copy(), dep=dep + 1)
                                        if used_alias:
                                            target_name_alias = used_alias
                                        candidate_file_paths.update(source_set)
                            elif len(self.non_attr_import_graph[_current_file][target_name]) != 0:
                                import_files = self.non_attr_import_graph[_current_file][target_name]
                                for import_file in import_files:
                                    if import_file not in visited_set:
                                        source_set, used_alias = add_name_constraint(import_file, target_name_alias if target_name_alias else target_name, visited_set.copy(), dep=dep + 1)
                                        if used_alias:
                                            target_name_alias = used_alias
                                        candidate_file_paths.update(source_set)
                            # 第四种情况：当前文件中使用 wildcard import
                            elif _current_file in self.ekstazip.using_wildcard_import:
                                candidate_file_paths.add("*")

                            # using all possible definitions
                            if "*" in candidate_file_paths:
                                candidate_file_paths = {"*"}

                            # fallback
                            # TODO: 感觉这个 fallback 不太好！但是 scikit-learn 29b379a7624afe4de5fb62a2fc151662d2933c88)->4b79fdf17b7fdc2237999198c446acb15c341032 依赖于这个
                            if len(candidate_file_paths) == 0:
                                candidate_file_paths.add("*")

                            if target_name_alias:
                                self.non_attr_file_constrain_dict[_current_file][target_name_alias] = candidate_file_paths
                                self.used_alias_mapping[_current_file][target_name] = target_name_alias
                            else:
                                self.non_attr_file_constrain_dict[_current_file][target_name] = candidate_file_paths

                            return candidate_file_paths, target_name_alias

                        _, alias = add_name_constraint(file_path, name, set())
                        if alias:
                            code_object.external_names.add(alias)
                            code_object.external_names.discard(name)

                if class_name is None:
                    continue
                # 给定一个 sure_attr，以及其所被使用的类，确定其可能的所有定义所在的类
                for attr in code_object.external_attributes:
                    if attr not in self.attr_class_constrain_dict[class_name]:
                        # 第一种情况：当前类中有定义
                        self.attr_class_constrain_dict[class_name][attr] = set()
                        if attr in self.defined_names.get(class_name, set()):
                            self.attr_class_constrain_dict[class_name][attr] = {class_name}

                        # 第二种情况：在全部超类和子类中找（over-approximate）
                        # TODO: 是否可以只找第一层超类？层层递进？
                        # TODO: possible efficiency issue
                        super_classes = get_super_sub_classes(class_name, "super")
                        sub_classes = get_super_sub_classes(class_name, "sub")
                        self.attr_class_constrain_dict[class_name][attr].update({_class_name for _class_name in super_classes | sub_classes if attr in self.defined_names.get(_class_name, set())})

        InstanceLogger().get_logger().info(f"[all_super_classes]\n{pprint.pformat(all_super_classes)}\n\n")
        InstanceLogger().get_logger().info(f"[all_sub_classes]\n{pprint.pformat(all_sub_classes)}\n\n")
        InstanceLogger().get_logger().info(f"[non_attr_file_constrain_dict]\n{pprint.pformat(self.non_attr_file_constrain_dict)}\n\n")
        InstanceLogger().get_logger().info(f"[attr_class_constrain_dict]\n{pprint.pformat(self.attr_class_constrain_dict)}\n\n")


    def handle_deleted_added_functions(self, cached_module_name_dict, module_name_dict):
        def get_all_identifiers(name_dict):
            identifiers = set()
            for _, _name_dict in name_dict.items():
                for _, obj_list in _name_dict.items():
                    for obj in obj_list:
                        if isinstance(obj, FunctionObject):
                            identifiers.add((obj.name, obj.file_path, obj.class_name))
            return identifiers

        previous_identifiers = get_all_identifiers(cached_module_name_dict)
        current_identifiers = get_all_identifiers(module_name_dict)
        deleted_identifiers = previous_identifiers - current_identifiers
        added_identifiers = current_identifiers - previous_identifiers

        # handle added identifiers
        for name, file_path, class_name in added_identifiers:
            self.added_functions[name].add((file_path, "None" if not class_name else class_name))
        InstanceLogger().get_logger().info(f"[added_functions]\n{pprint.pformat(self.added_functions, indent=4)}\n\n")

        # handle deleted identifiers
        for name, file_path, class_name in deleted_identifiers:
            # self.deleted_functions[name].add((file_path, "None" if not class_name else class_name))
            def add_deleted_functions_in_super_classes(_name, _file_path, _class_name, dep=0):
                if dep >= 100:
                    return
                current_classes = self.module_name_dict_all.get(_file_path, dict()).get(_class_name, set())
                current_classes = [c for c in current_classes if isinstance(c, ClassObject)]
                if len(current_classes) == 0:
                    return

                # TODO: 同名类？
                current_class = list(current_classes)[0]
                super_classes = current_class.super_classes
                for super_class in super_classes:
                    super_class_set = self.world_name_dict.get(super_class, set())
                    if len(super_class_set) == 0:
                        continue
                    super_class_obj = list(super_class_set)[0]
                    file_path_sc = super_class_obj.file_path

                    found = False
                    for func_in_file_sc in self.module_name_dict_all.get(file_path_sc, dict()).get(_name, set()):
                        if func_in_file_sc.class_name == super_class:
                            self.deleted_functions_in_sc[_name].add((file_path_sc, super_class))
                            found = True
                    if not found:
                        add_deleted_functions_in_super_classes(_name, file_path_sc, super_class, dep=dep + 1)

            add_deleted_functions_in_super_classes(name, file_path, class_name)

        InstanceLogger().get_logger().info(f"[deleted_identifiers]\n{pprint.pformat(deleted_identifiers, indent=4)}\n\n")
        InstanceLogger().get_logger().info(f"[deleted_functions_in_sc]\n{pprint.pformat(self.deleted_functions_in_sc, indent=4)}\n\n")

    def get_tests_to_run(self, init: bool = False):
        tests_to_run_naive = self.ekstazip.get_tests_to_run(naive=False)

        with Timer("Loading caches") as _:
            cache_path = os.path.join(self.project_path, "nbdp_cache.json")
            cached_module_name_dict_all = dict()
            if os.path.exists(cache_path):
                with open(cache_path, "r") as cache_file:
                    cached_module_name_dict_all = json.load(cache_file, object_hook=json_load_object_hook)

        # STEP2: parsing .pyc files incrementally
        with Timer("Parsing .pyc files") as _:
            all_pycs = get_all_pycs(self.project_path)
            for pyc in all_pycs:
                py = pyc_to_py(pyc)
                if pyc_to_py(py) in cached_module_name_dict_all and py not in self.ekstazip.changed_files:
                    module_name_dict = cached_module_name_dict_all[py]
                    module_object = module_name_dict["<module>"][0]
                else:
                    file_content = self.ekstazip.get_file_content(py)
                    parsed_tree = self.ekstazip.get_parsed_tree(py)
                    module_object, module_name_dict = parse_pyc(pyc, self.project_path, file_content, parsed_tree)
                # module_name_dict = self.unset_import_alias(module_name_dict)
                self.world_pyc_dict[py] = module_object
                self.module_name_dict_all[py] = module_name_dict

            # merge name dict of different pycs
            self.world_name_dict = merge_dict_list(self.module_name_dict_all.values())
            self.build_init_mapping()
            self.make_non_attr_import_graph()
            checksum_counts = self.build_names_set()
            self.remove_invalid_names()
            self.handle_deleted_added_functions(cached_module_name_dict_all, self.module_name_dict_all)

        with Timer("Dumping caches and preparing") as _:
            # with open(os.path.join(self.project_path, "nbdp_cache_old.json"), "w") as cache_file:
            #     json.dump(self.world_name_dict, cache_file, default=json_dump_default, indent=4)
            with open(cache_path, "w") as cache_file:
                json.dump(self.module_name_dict_all, cache_file, default=json_dump_default, indent=4)

            # if not cached_module_name_dict_all:
            #     return tests_to_run_naive

            self.add_global_variables()
            self.build_d_hash_mapping()
            self.build_registered_classes()
            self.build_defined_names()
            self.build_constrain_dicts()

        # attributes: could be non-attributes
        # names(original): never have xxx_ATTR, must not be attributes
        # self.resolve_external_boths()
        # now non-attr and attributes and strict

        # 检查是否出现了新的 decorator。如果是
        cached_all_decorator_checksums = set()
        for name_dict in cached_module_name_dict_all.values():
            for obj_list in name_dict.values():
                for obj in obj_list:
                    if isinstance(obj, FunctionObject) and obj.decorator_hash:
                        cached_all_decorator_checksums.add(obj.decorator_hash)
        current_all_decorator_checksums = self.decorator_hash_2_elements.keys()
        new_d_checksums = current_all_decorator_checksums - cached_all_decorator_checksums
        if not init and new_d_checksums:

            InstanceLogger().get_logger().warning(f"New decorator checksums detected, fall back to EkstaziP. New checksums: "
                                                  f"\n{pprint.pformat(new_d_checksums)}")
            print(f"[NameBDP] Tests to run ({len(tests_to_run_naive)}/{len(self.ekstazip.all_test_paths)})")
            InstanceLogger().get_logger().info(
                f"Tests to run ({len(tests_to_run_naive)}/{len(self.ekstazip.all_test_paths)}): \n{pprint.pformat(tests_to_run_naive)}")
            return tests_to_run_naive

        # # STEP3: collect entries
        # with Timer("Collecting test entries") as _:
        #     entries = collect_all_heuristic(self.project_path)
        #     # entries = [pyc_path for pyc_path in all_pycs if pyc_to_py(pyc_path) in test_files]
        #     InstanceLogger().get_logger().info(f"Collected entries: \n{pprint.pformat(entries)}\n\n")
        #
        #     # if cached_module_name_dict_all does not exist, return
        #     if not cached_module_name_dict_all:
        #         InstanceLogger().get_logger().info("No cache found, returning all test entries...")
        #         return entries

        # STEP4: find changed elements
        def get_checksum_name_dict(name_dict: dict):
            InstanceLogger().get_logger().info(f"get_checksum_name_dict() input: \n{pprint.pformat(name_dict)}")
            merged_list = list(itertools.chain.from_iterable(name_dict.values()))
            checksums = {obj.checksum for obj in merged_list if hasattr(obj, 'checksum') and obj.checksum is not None
                         and not is_test_file_pytest(obj.file_path, self.project_path)}
            for obj in merged_list:
                if hasattr(obj, 'local_variables'):
                    checksums.update({v["checksum"] for v in obj.local_variables.values()})
            InstanceLogger().get_logger().info(f"get_checksum_name_dict() output: \n{pprint.pformat(checksums)}")
            return checksums

        with Timer("Find changed elements") as _:
            different_checksums = set()
            if not init:
                for changed_file in self.ekstazip.changed_files:
                    if changed_file in self.module_name_dict_all:
                        current_checksums = get_checksum_name_dict(self.module_name_dict_all[changed_file])
                        if changed_file in cached_module_name_dict_all:
                            # TODO: what if some element is deleted?
                            previous_checksums = get_checksum_name_dict(cached_module_name_dict_all[changed_file])
                            different_checksums.update(current_checksums - previous_checksums)
                        else:
                            different_checksums.update(current_checksums)
                    else:
                        # TODO: what if some file is deleted?
                        pass
            InstanceLogger().get_logger().info(f"Checksums for modified elements: \n{pprint.pformat(different_checksums)}")

            # 如果一个函数被删除：找到超类的函数并作为被修改函数
            deleted_funcs_in_sc_checksums = set()
            for func_name, source_list in self.deleted_functions_in_sc.items():
                for file_path, class_name in source_list:
                    for func_obj in self.module_name_dict_all.get(file_path, dict()).get(func_name, set()):
                        if func_obj.class_name == class_name:
                            deleted_funcs_in_sc_checksums.add(func_obj.checksum)
            InstanceLogger().get_logger().info(f"[deleted_funcs_in_sc_checksums]: \n{pprint.pformat(deleted_funcs_in_sc_checksums)}")
            different_checksums.update(deleted_funcs_in_sc_checksums)

        # STEP5: name propagation
        with Timer("Running name-based dependency propagation") as _:
            tests_to_run = []
            tasks = []
            for e in tests_to_run_naive:
            # for e in self.ekstazip.all_test_paths:
                if e in self.ekstazip.changed_files and not init:
                    tests_to_run.append(e)
                    continue
                if e not in self.world_pyc_dict:
                    continue

                tasks.append(e)

            if SELECT_USE_PARALLEL:
                with Pool(
                        processes=40,
                        initializer=_init_pool,
                        initargs=(self, different_checksums, init)
                ) as pool:
                    for e, dependent_checksums, found_definitions, log_output, iter_count in pool.imap_unordered(
                            _run_algorithm, tasks
                    ):
                        # dependent_checksums, found_definitions, log_output, iter_count = self.algorithm(e, module_object, different_checksums, init)
                        InstanceLogger().get_logger().info(log_output)
                        self.iter_counts.append(iter_count)
                        for name, count in found_definitions.items():
                            self.found_definitions[name] += count
                        InstanceLogger().get_logger().info(f"Dependent checksums for {e}: {len(dependent_checksums)}/{checksum_counts}")

                        if dependent_checksums & different_checksums:
                            tests_to_run.append(pyc_to_py(e))
            else:
                _init_pool(self, different_checksums, init)
                for e in tasks:
                    e, dependent_checksums, found_definitions, log_output, iter_count = _run_algorithm(e)
                    InstanceLogger().get_logger().info(log_output)
                    self.iter_counts.append(iter_count)
                    for name, count in found_definitions.items():
                        self.found_definitions[name] += count
                    InstanceLogger().get_logger().info(
                        f"Dependent checksums for {e}: {len(dependent_checksums)}/{checksum_counts}"
                    )

                    if dependent_checksums & different_checksums:
                        tests_to_run.append(pyc_to_py(e))

            print(f"[NameBDP] Tests to run ({len(tests_to_run)}/{len(self.ekstazip.all_test_paths)})")
            InstanceLogger().get_logger().info(f"Tests to run ({len(tests_to_run)}/{len(self.ekstazip.all_test_paths)}): {pprint.pformat(tests_to_run)}")

            avg_iters = sum(self.iter_counts) / len(self.iter_counts) if self.iter_counts else 0
            InstanceLogger().get_logger().info(f"Name-based dependency propagation takes {avg_iters} iterations on average.")

            if init:
                most_found = sorted([(name, count) for name, count in self.found_definitions.items()],
                                    key=lambda i: -i[1])[:config.NUM_DYNAMIC_MONITOR]
                InstanceLogger().get_logger().info(f"Names with the most definitions found: \n{pprint.pformat(most_found, indent=4)}")
                self.critical_names = {n[0] for n in most_found}
                with open(self.critical_names_path, "w") as f:
                    json.dump(list(self.critical_names), f, indent=4)
                return self.ekstazip.all_test_paths
        return tests_to_run


    def run_and_update(self, tests_to_run):
        file_backups, added_lines = self.instrument_monitored_functions()
        self.ekstazip.run_and_update(tests_to_run, added_lines)
        self.file_recover(file_backups)


    def main(self):
        tests_to_run = self.get_tests_to_run()
        self.run_and_update(tests_to_run)

