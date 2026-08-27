import dis
import hashlib
import marshal
import opcode
import os
import pprint
import re
from collections import defaultdict
from types import CodeType
from collections.abc import Set, Mapping, Sequence, Collection
from abc import ABC, abstractmethod
from typing import List, Optional, Set

import xxhash

from src.config import REGISTRY_DECORATOR_KEYWORDS
from src.utils import pyc_to_py, is_test_file_pytest, get_all_str_consts, find_root_package
from src.instance_logger import InstanceLogger
from src.parser import TreeSitterClient


class CodeObject(ABC):
    def __init__(self, name, file_path, external_names, external_attributes, external_boths):
        self.file_path = file_path
        self.name = name
        self.external_names = set(external_names)
        self.external_attributes = set(external_attributes)
        self.external_boths = set(external_boths)

    @abstractmethod
    def __repr__(self):
        pass

    def to_dict(self):
        return {
            "external_names": list(self.external_names),
            "external_attributes": list(self.external_attributes),
            "external_boths": list(self.external_boths),
            "file_path": self.file_path,
            "name": self.name
        }


class VariableObject(CodeObject):
    def __init__(self, name, file_path, external_names, external_attributes, external_boths, checksum, class_name):
        super().__init__(name, file_path, external_names, external_attributes, external_boths)
        self.checksum = checksum
        self.class_name = class_name

    def __repr__(self):
        return "Variable [{} in {}]".format(self.name, self.file_path)

EXCLUDE_DEF_NAMES = {
    "__doc__", "__name__", "__module__", "__firstlineno__", "__qualname__", "__version__", "__all__", "__annotations__"
}
class ModuleObject(CodeObject):

    @staticmethod
    def make_object(code_type_wrapper, pyc_path, local_variables, injected_fixtures, external_names, external_attrs, external_boths):
        co = code_type_wrapper.co

        py_path = pyc_to_py(pyc_path)
        if is_test_file_pytest(py_path, ""):
            __external_names, __external_attributes, __external_boths = get_all_names(code_type_wrapper)
            if "methodcaller" in __external_boths or "methodcaller" in __external_names:
                const_strs = get_all_str_consts(co)
                InstanceLogger().get_logger().info(f"methodcaller identified in {py_path}, adding all const strs: \n{pprint.pformat(const_strs, indent=4)}\n\n")
                __external_boths.update(const_strs)
            return ModuleObject(co.co_name, py_path, __external_names, __external_attributes, __external_boths, dict(), injected_fixtures)
        else:
            return ModuleObject(co.co_name, py_path, external_names, external_attrs, external_boths, local_variables, set())

    def __init__(self, name, file_path, external_names, external_attributes, external_boths, local_variables, injected_fixtures):
        self.local_variables = local_variables
        self.injected_fixtures = injected_fixtures
        super().__init__(name, file_path, external_names, external_attributes, external_boths)

    def __repr__(self):
        return "Module [{} in {}]".format(self.name, self.file_path)

    def to_dict(self):
        data = super().to_dict()
        data["type"] = "ModuleObject"
        data["local_variables"] = self.local_variables
        data["injected_fixtures"] = list(self.injected_fixtures)
        return data


def naive_instr_to_tuple_list(instr_list: List[dis.Instruction], handling_local: bool = False) -> list:
    def _naive_instr_to_tuple(instr):
        if isinstance(instr.argval, CodeType):
            return instr.opname, f'<code object {instr.argval.co_name}>'
        if handling_local and "JUMP" in instr.opname:
            return instr.opname, 0
        return instr.opname, instr.argval
    return [_naive_instr_to_tuple(instr) for instr in instr_list]


def has_registry_decorator(instr_tuples):
    return any(isinstance(i[1], str) and any(keyword in i[1] for keyword in REGISTRY_DECORATOR_KEYWORDS) for i in instr_tuples)


class FunctionObject(CodeObject):
    @staticmethod
    def make_object(code_type_wrapper, pyc_path, instructions, class_name, pre_instr, post_instr, decorator_instr_list: List[dis.Instruction], body_starts_line):
        # TODO: is dfs necessary?
        # TODO: dfs may introduce unnecessary names, e.g. nonlocal variables

        co = code_type_wrapper.co
        __external_names, __external_attributes, __external_boths = get_all_names(code_type_wrapper)
        pre_instr_tuples = naive_instr_to_tuple_list(pre_instr)
        post_instr_tuples = naive_instr_to_tuple_list(post_instr)

        # 后面可能会跟一些奇奇怪怪的冗余信息
        if ("STORE_NAME", co.co_name) in post_instr_tuples:
            idx = post_instr_tuples.index(("STORE_NAME", co.co_name))
            post_instr_tuples = post_instr_tuples[:idx + 1]

        instructions = pre_instr_tuples + list(instructions) + post_instr_tuples
        __checksum = instr2checksum(instructions, pyc_path, co.co_name)

        extra_instr = pre_instr + post_instr
        for idx, instr in enumerate(extra_instr):
            if idx == 0:
                previous_instr = None
            else:
                previous_instr = extra_instr[idx - 1]
            name, attr, both = get_used_names_instr(instr, previous_instr, no_def=True)
            if name is not None:
                __external_names.add(name)
            if attr is not None:
                __external_attributes.add(attr)
            if both is not None:
                __external_boths.add(both)

        decorator_hash = None
        if decorator_instr_list:
            instr_tuples = naive_instr_to_tuple_list(decorator_instr_list)
            if has_registry_decorator(instr_tuples):
                instr_tuples.append(("FUNC_IDENTIFIER", f"{class_name}::{co.co_name}"))
                decorator_hash = instr2checksum(instr_tuples, pyc_path, f"decorator for {co.co_name}")

        return FunctionObject(co.co_name, pyc_to_py(pyc_path), __external_names, __external_attributes, __external_boths, class_name, __checksum, decorator_hash, body_starts_line)

    def __init__(self, name, file_path, external_names, external_attributes, external_boths, class_name, checksum, decorator_hash, body_starts_line):
        super().__init__(name, file_path, external_names, external_attributes, external_boths)
        self.class_name = class_name
        self.checksum = checksum
        self.decorator_hash = decorator_hash
        self.body_starts_line = body_starts_line

    def __repr__(self):
        if self.class_name:
            return "Function [{}.{} in {}]".format(self.class_name, self.name, self.file_path)
        else:
            return "Function [{} in {}]".format(self.name, self.file_path)

    def to_dict(self):
        data = super().to_dict()
        data["class_name"] = self.class_name
        data["checksum"] = self.checksum
        data["decorator_hash"] = self.decorator_hash
        data["type"] = "FunctionObject"
        data["body_starts_line"] = self.body_starts_line
        return data


class ClassObject(CodeObject):
    @staticmethod
    def make_object(code_type_wrapper, pyc_path, super_classes: Set[str], local_variables, decorator_instr_list):
        co = code_type_wrapper.co
        __external_names = super_classes

        is_registered = False
        if decorator_instr_list:
            instr_tuples = naive_instr_to_tuple_list(decorator_instr_list)
            if has_registry_decorator(instr_tuples):
                is_registered = True

        return ClassObject(co.co_name, pyc_to_py(pyc_path), __external_names, set(), set(), super_classes, local_variables, is_registered)

    def __init__(self, name, file_path, external_names, external_attributes, external_boths, super_classes: Set[str], local_variables, is_registered):
        super().__init__(name, file_path, external_names, external_attributes, external_boths)
        self.super_classes = super_classes
        self.local_variables = local_variables
        self.is_registered = is_registered

    def __repr__(self):
        if self.super_classes:
            return "Class [{}({}) in {}]".format(self.name, ", ".join(self.super_classes), self.file_path)
        else:
            return "Class [{} in {}]".format(self.name, self.file_path)

    def to_dict(self):
        data = super().to_dict()
        data["super_classes"] = list(self.super_classes)
        data["local_variables"] = self.local_variables
        data["is_registered"] = self.is_registered
        data["type"] = "ClassObject"
        return data


def json_dump_default(obj):
    if isinstance(obj, CodeObject):
        return obj.to_dict()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def json_load_object_hook(d: dict):
    obj_type = d.get("type", None)
    if obj_type == "ModuleObject":
        return ModuleObject(
            name=d["name"],
            file_path=d["file_path"],
            external_names=set(d["external_names"]),
            external_attributes=set(d["external_attributes"]),
            external_boths=set(d["external_boths"]),
            local_variables=d["local_variables"],
            injected_fixtures=set(d["injected_fixtures"]),
        )
    elif obj_type == "FunctionObject":
        return FunctionObject(
            name=d["name"],
            file_path=d["file_path"],
            external_names=set(d["external_names"]),
            external_attributes=set(d["external_attributes"]),
            external_boths=set(d["external_boths"]),
            class_name=d["class_name"],
            checksum=d["checksum"],
            decorator_hash=d["decorator_hash"],
            body_starts_line=d["body_starts_line"]
        )
    elif obj_type == "ClassObject":
        return ClassObject(
            name=d["name"],
            file_path=d["file_path"],
            external_names=set(d["external_names"]),
            external_attributes=set(d["external_attributes"]),
            external_boths=set(d["external_boths"]),
            super_classes=set(d["super_classes"]),
            local_variables=d["local_variables"],
            is_registered=d["is_registered"],
        )
    return d


class CodeTypeWrapper:
    def __init__(self, co):
        self.co = co
        self.raw_instructions = list(dis.get_instructions(co))
        self.children = []


def get_all_names(module_object: CodeTypeWrapper):
    # TODO: need a finer-grained method
    names = set()
    attributes = set()
    boths = set()
    for obj in module_object.children:
        all_names, all_attributes, all_boths = get_all_names(obj)
        names.update(all_names)
        attributes.update(all_attributes)
        boths.update(all_boths)
    instructions = module_object.raw_instructions
    for idx, instr in enumerate(instructions):
        if idx == 0:
            previous_instr = None
        else:
            previous_instr = instructions[idx - 1]
        name, attr, both = get_used_names_instr(instr, previous_instr)
        if name is not None:
            names.add(name)
        if attr is not None:
            attributes.add(attr)
        if both is not None:
            boths.add(both)
    return names, attributes, boths


def get_header_size_from_filename(pyc_path: str):
    # TODO: make sure the head size is right!
    match = re.search(r"cpython-(\d{2,3})", pyc_path)
    if match:
        version_num = int(match.group(1))  # 如 36, 37, 310, 311
        major = version_num // 10 if version_num < 100 else version_num // 100
        minor = version_num % 10 if version_num < 100 else int(str(version_num)[-2:])
        if major == 3 and minor >= 7:
            return 16
    return 12


def load_code_from_pyc(pyc_path: str):
    header_size = get_header_size_from_filename(pyc_path)
    with open(pyc_path, "rb") as f:
        f.seek(header_size)
        try:
            code = marshal.load(f)
        except Exception as e:
            print(f"Error loading {pyc_path}: {e}")
            return None
    return code


def canonical_repr(val):
    # TODO: may need a more general method
    if isinstance(val, Set):
        return str(type(val)) + "({" + ", ".join(sorted(canonical_repr(item) for item in val)) + "})"
    elif isinstance(val, Mapping):
        items = sorted((canonical_repr(k), canonical_repr(v)) for k, v in val.items())
        return str(type(val)) + "({" + ", ".join(f"{k}: {v}" for k, v in items) + "})"
    elif isinstance(val, Sequence) and not isinstance(val, str):
        bracket_open = "[" if isinstance(val, list) else "("
        bracket_close = "]" if isinstance(val, list) else ")"
        return str(type(val)) + "(" + bracket_open + ", ".join(canonical_repr(item) for item in val) + bracket_close + ")"
    else:
        return repr(val)


EXCLUDE_OPS = {
    "EXTENDED_ARG"
}
def instr2checksum(instructions: list, pyc_path: str, obj_name: str):
    content = "\n".join(f"{op}:{canonical_repr(val)};" for op, val in instructions if str(op) not in EXCLUDE_OPS)
    InstanceLogger().get_logger().info(f"Parsed {obj_name} in {pyc_path}: \n{content}\n\n\n")
    # return hashlib.md5(content.encode("utf-8")).hexdigest()
    return xxhash.xxh3_64(content.encode("utf-8")).hexdigest()

ROOT_PACKAGE = ""
def get_used_names_instr(instr: dis.Instruction, previous_instr: Optional[dis.Instruction], no_def: bool = False):
    if instr.opcode not in opcode.hasname or not isinstance(instr.argval, str):
        return None, None, None
    op = instr.opcode
    opname = instr.opname

    if no_def and opname == "STORE_NAME":
        return None, None, None

    attr, name, both = None, None, None
    if op in opcode.hasname:
        if opname.endswith("_ATTR"):
            attr_hint = ["self", "cls"]
            if previous_instr and previous_instr.argval and ((isinstance(previous_instr.argval, str) and previous_instr.argval in attr_hint) or
                                                             (isinstance(previous_instr.argval, Collection) and any(s in str(previous_instr.argval) for s in attr_hint))):
                attr = instr.argval
            elif ROOT_PACKAGE and previous_instr and previous_instr.argval and ((isinstance(previous_instr.argval, str) and previous_instr.argval == ROOT_PACKAGE) or
                                                             (isinstance(previous_instr.argval, Collection) and ROOT_PACKAGE in str(previous_instr.argval))):
                both = f"?{instr.argval}"
            else:
                both = instr.argval
        else:
            name = instr.argval
    return name, attr, both


def local_instr_get_use_def(local_instr: List[dis.Instruction]):
    use_names = set()
    use_attrs = set()
    use_boths = set()
    def_names = set()
    for idx, instr in enumerate(local_instr):
        if idx == 0:
            previous_instr = None
        else:
            previous_instr = local_instr[idx - 1]
        name, attr, both = get_used_names_instr(instr, previous_instr)
        if instr.opname.startswith("STORE"):
            if name is not None:
                def_names.add(name)
        else:
            if name is not None:
                use_names.add(name)
            if attr is not None:
                use_attrs.add(attr)
            if both is not None:
                use_boths.add(both)
    return use_names, use_attrs, use_boths, def_names


def get_instr_line(instr):
    return instr.line_number if hasattr(instr, 'line_number') else instr.starts_line


def parse_pyc(pyc_path: str, project_path: str, file_content: str, parsed_tree):
    global ROOT_PACKAGE
    py_path = pyc_to_py(os.path.join(project_path, pyc_path))
    root_package_name = find_root_package(project_path, py_path)
    ROOT_PACKAGE = root_package_name
    InstanceLogger().get_logger().info(f"Parsing {py_path} in package {root_package_name}...")

    file_lines = [str(s) for s in file_content.splitlines(keepends=True)]
    all_statements = TreeSitterClient().get_all_statements(parsed_tree.root_node)

    start_line_to_stm_lines = defaultdict(set)
    stm_line_to_start_line = dict()
    for stm in all_statements:
        # tree-sitter 的行号是从 0 开始的，所以需要 + 1
        start_line = stm.start_point[0] + 1
        end_line = stm.end_point[0] + 1
        lines = set(range(start_line, end_line + 1))
        start_line_to_stm_lines[start_line] = lines
        for l in lines:
            stm_line_to_start_line[l] = start_line

    def parse_code_obj(co: CodeType, code_type: str, pre_instr: list, post_instr: list, decorator_instr_list: list, body_starts_line: int, class_name: str = None, super_classes: Set[str] = None, inner_func: bool = False):
        nonlocal co_dict
        instructions = []
        is_class_def = False
        code_type_wrapper = CodeTypeWrapper(co)
        instructions_raw = code_type_wrapper.raw_instructions

        last_line = get_instr_line(instructions_raw[0])
        if last_line is None:
            last_line = body_starts_line - 1
        line_2_idx = defaultdict(list)
        idx_2_line = dict()
        for idx, instr in enumerate(instructions_raw):
            line = get_instr_line(instr)
            if line is None:
                line = last_line
            else:
                last_line = line
            line_2_idx[line].append(idx)
            idx_2_line[idx] = line

        # # 修正：对于空的函数定义（不具有函数体，因此不可能被调用），不进行记录
        # if code_type == "function" and max(line_2_idx.keys(), default=body_starts_line - 1) <= body_starts_line - 1:
        #     inner_func = True

        new_body_starts_line = min({l for l in line_2_idx.keys() if l > body_starts_line - 1}, default=-1)
        if new_body_starts_line == -1:
            InstanceLogger().get_logger().warning(f"Body not found for {co.co_name} in {pyc_path}({body_starts_line})")
        else:
            body_starts_line = new_body_starts_line

        idx_exclude = set()
        # idx_handled_import = set()
        for idx, instr in enumerate(instructions_raw):
            if instr.opname == "LOAD_BUILD_CLASS":
                is_class_def = True
            # if instr.opname.startswith("IMPORT") and idx not in idx_handled_import and not using_wildcard_import:
            #     line = idx_2_line[idx]  # import 所在行
            #     main_idx = line_2_idx[line]  # import 所在行对应所有 idx
            #     idx_handled_import.update(set(main_idx))
            #     for _idx in main_idx:
            #         _instr = instructions_raw[_idx]
            #         if _instr.opname == "LOAD_CONST" and isinstance(_instr.argval, Collection) and "*" in _instr.argval:
            #             using_wildcard_import = True
            #             break
            #         if _instr.opname in ["STORE_NAME", "STORE_FAST"]:
            #             stored_name = str(_instr.argval)
            #             if _idx != 0 and (import_instr := instructions_raw[_idx-1]).opname.startswith("IMPORT_"):
            #                 import_name = import_instr.argval
            #                 if import_name != stored_name:
            #                     InstanceLogger().get_logger().warning(f"Import alias of {import_name} -> {stored_name} in {pyc_path}::{line}")
            #                     import_alias[stored_name] = import_name
            #                     stored_name = import_name
            #             import_name_graph[stored_name].add(line)

            if instr.opname == "LOAD_CONST" and isinstance(instr.argval, CodeType):
                argval_co_name = instr.argval.co_name
                instructions.append((instr.opname, f'<code object {argval_co_name}>'))

                # find super classes for class definition
                _super_classes = set()
                if is_class_def:
                    for i in range(idx + 1, len(instructions_raw)):
                        current_instr = instructions_raw[i]
                        if current_instr.opname.startswith("STORE_") and current_instr.argval == argval_co_name:
                            InstanceLogger().get_logger().info(f"Superclasses of {argval_co_name}: {pprint.pformat(_super_classes)}")
                            break
                        if i == 0:
                            previous_instr = None
                        else:
                            previous_instr = instructions_raw[i - 1]
                        name, attr, both = get_used_names_instr(current_instr, previous_instr)
                        current_name = name or attr or both
                        if isinstance(current_name, str) and current_name != argval_co_name:
                            _super_classes.add(current_name)
                        assert i != len(instructions_raw) - 1, f"STORE_xxx: '{argval_co_name}' unfound for class definition"

                # instructions before/after the LOAD_CONST that belong to the sub-element
                line = idx_2_line[idx]  # def 所在行
                main_idx = line_2_idx[line]     # def 所在行对应所有 idx
                all_relevant_idx = sorted(list(range(min(main_idx), max(main_idx) + 1)))    # def 所在行对应所有 idx 之间的 idx
                all_lines = sorted(list({idx_2_line[_idx] for _idx in all_relevant_idx}))   # def 所在行对应所有 idx 之间的 idx 对应的 行
                all_idx = set.union(*(set(line_2_idx[l]) for l in all_lines))

                # 特殊情况：前面的 instr，行号却大于当前行，说明当前行依赖于前面这些行
                for _idx in range(idx, -1, -1):
                    _line = idx_2_line[_idx]
                    if _line >= line:
                        all_idx.add(_idx)
                    else:
                        break

                all_idx = sorted(list(all_idx))
                idx_exclude.update(all_idx)

                pre_instr_idx = [_idx for _idx in range(all_idx[0], idx)]
                post_instr_idx = [_idx for _idx in range(idx + 1, all_idx[-1] + 1)]

                decorator_list = []
                if pre_instr_idx:
                    start_line = min([idx_2_line[_idx] for _idx in pre_instr_idx])
                    if start_line < line:
                        for l in range(start_line, line):
                            decorator_list += [instructions_raw[_idx] for _idx in line_2_idx[l] if _idx in pre_instr_idx]

                    InstanceLogger().get_logger().info(f"Decorator instructions of {argval_co_name} at line {line}: "
                                                       f"{pprint.pformat(naive_instr_to_tuple_list(decorator_list))}")

                # print(instr.argval.co_name)
                # print(f"line: {line}")
                # print(f"main_idx: {main_idx}")
                # print(f"all_relevant_idx: {all_relevant_idx}")
                # print(f"all_lines: {all_lines}")
                # print(f"pre_instr_idx: \n{pprint.pformat(pre_instr_idx)}")
                # print(f"post_instr_idx: \n{pprint.pformat(post_instr_idx)}")
                # print(f"decorator_list: \n{pprint.pformat(decorator_list)}")
                # print("\n\n\n")

                sub_instructions, sub_code_obj, sub_code_wrapper = parse_code_obj(
                    co=instr.argval,
                    code_type="class" if is_class_def else "function",
                    class_name=co.co_name if code_type=="class" else None,
                    super_classes=_super_classes,
                    inner_func=inner_func or code_type == "function",
                    pre_instr = [instructions_raw[_idx] for _idx in pre_instr_idx],
                    post_instr = [instructions_raw[_idx] for _idx in post_instr_idx],
                    decorator_instr_list=decorator_list,
                    body_starts_line=line + 1
                )
                is_class_def = False
                instructions.extend(sub_instructions)
                code_type_wrapper.children.append(sub_code_wrapper)

                continue
            instructions.append((instr.opname, instr.argval))

            # 修正：EXCLUDE_DEF_NAMES 中的名字是自动生成的，不影响语义，因此在 instructions 中将这些值抹平，以免计算 modified checksum 时出现 false positive
            # TODO: 可能需要一种更加优雅的方式
            if instr.opname == "STORE_NAME" and instr.argval in EXCLUDE_DEF_NAMES and \
                    len(instructions) >= 2 and instructions[-2][0] == "LOAD_CONST":
                instructions[-2] = ("LOAD_CONST", 0)

            # 修正：对于空的函数定义（不具有函数体，因此不可能被调用），不进行记录
            if code_type == "function" and instr.opname == "RETURN_CONST" and str(instr.argval) == "None":
                line = idx_2_line[idx]
                if 1 <= line <= len(file_lines) and "..." in file_lines[line - 1]:
                    inner_func = True

            # 修正：__static_attributes__ 是一个暂时没有被使用的 dummy，直接忽略他
            if instr.opname == "STORE_NAME" and instr.argval == "__static_attributes__":
                idx_exclude.update({idx, idx - 1})

            # 修正：类定义最后会有 RETURN_CONST:None，不会造成任何影响
            if instr.opname == "RETURN_CONST":
                idx_exclude.add(idx)

        build_co = None
        if not inner_func and co.co_name not in ["<listcomp>", "<genexpr>", "<lambda>", "<setcomp>", "<dictcomp>"]:
            def get_local_variables(handle_class: bool = False):
                # expand idx_exclude: remove import statements related instructions
                for idx, instr in enumerate(instructions_raw):
                    if instr.opname.startswith("IMPORT_") or \
                            (instr.opname.startswith("STORE_") and instr.argval in EXCLUDE_DEF_NAMES):
                        idx_exclude.update(set(line_2_idx[idx_2_line[idx]]))

                _local_instr_idx = [_idx for _idx in range(0, len(instructions_raw)) if
                                        _idx not in idx_exclude]

                defined_names = set()
                used_name_to_start_lines = defaultdict(set)
                relevant_stm_start_lines = set()
                # 第一步：找到所有定义的全局变量，并提取每一行所使用的 name
                for _idx in _local_instr_idx:
                    if _idx == 0:
                        __previous_instr = None
                    else:
                        __previous_instr = instructions_raw[_idx - 1]
                    __instr = instructions_raw[_idx]
                    __name, __attr, __both = get_used_names_instr(__instr, __previous_instr)
                    __line = idx_2_line[_idx]
                    if __line in stm_line_to_start_line:
                        relevant_stm_start_lines.add(stm_line_to_start_line[__line])

                    if __name is not None:
                        if __line in stm_line_to_start_line:
                            used_name_to_start_lines[__name].add(stm_line_to_start_line[__line])
                        if __instr.opname.startswith("STORE_"):
                            defined_names.add(__name)

                    if __instr.opname == "STORE_SUBSCR":
                        load_name = ""
                        for __idx in sorted(line_2_idx[idx_2_line[_idx]], reverse=True):
                            ___instr = instructions_raw[__idx]
                            if ___instr.opname == "LOAD_NAME":
                                load_name = ___instr.argval
                                break
                        if load_name:
                            defined_names.add(load_name)

                # 遍历该区域的 statements，确认每个 defined_name 所对应的被使用的所有 idx，并判断该 statement 中是否存在副作用函数调用
                external_names = set()
                external_attrs = set()
                external_boths = set()
                start_line_to_names = dict()
                start_line_to_attrs = dict()
                start_line_to_boths = dict()
                for _start_line in relevant_stm_start_lines:
                    _lines = sorted(list(start_line_to_stm_lines[_start_line]))
                    # 该 statement 对应所有行对应所有 instr 的 idx
                    current_idxes = sorted(list(set().union(*[line_2_idx[l] for l in _lines])))
                    dependent_instr = [instructions_raw[__idx] for __idx in current_idxes if idx_2_line[__idx] >= body_starts_line]
                    _use_names, _use_attrs, _use_boths, _ = local_instr_get_use_def(dependent_instr)

                    start_line_to_names[_start_line] = _use_names
                    start_line_to_attrs[_start_line] = _use_attrs
                    start_line_to_boths[_start_line] = _use_boths

                    # 当前 statement 可能存在副作用函数调用
                    if ("conftest.py" not in py_path and
                            any("CALL" in ____instr.opname for ____instr in dependent_instr) and
                        not any("STORE" in ____instr.opname for ____instr in dependent_instr)):

                        # 特别的，如果这个 CALL 指令对应行的第一个 LOAD_NAME 对应一个本地变量，说明其只是在更新一个本地变量，而不是执行副作用功能
                        all_are_calling_attr = True
                        for ___idx in current_idxes:
                            _current_instr = instructions_raw[___idx]
                            if "CALL" in _current_instr.opname and not instructions_raw[min(line_2_idx[idx_2_line[___idx]])].argval in defined_names:
                                all_are_calling_attr = False
                        if all_are_calling_attr:
                            continue

                        # TODO: how to handle class?
                        if not handle_class:
                            side_effect_lines = "".join([file_lines[___l - 1] for ___l in _lines])

                            # TODO: this is so ugly
                            if "if __name__ == \"__main__\":" in side_effect_lines:
                                continue
                            InstanceLogger().get_logger().info(f"Detected side-effect function call in {py_path}: \n{side_effect_lines}\n\n")
                            external_names.update(_use_names)
                            external_attrs.update(_use_attrs)
                            external_boths.update(_use_boths)

                defined_names -= EXCLUDE_DEF_NAMES
                _local_variables = dict()
                for defined_name in defined_names:
                    # _used_idx_lines = {idx_2_line[i] for i in _used_idx}
                    #
                    # # 特殊情况：前面的 instr，行号却大于当前行，说明当前行依赖于前面这些行
                    # for l in _used_idx_lines:
                    #     max_idx = max(line_2_idx[l])
                    #     _used_idx.update({i for i in _local_instr_idx if i < max_idx and idx_2_line[i] > l})
                    # _used_idx = sorted(list(_used_idx))
                    #
                    # dependent_instr = [instructions_raw[__idx] for __idx in _used_idx if idx_2_line[__idx] >= body_starts_line]
                    # _use_names, _use_attrs, _use_boths, _ = local_instr_get_use_def(dependent_instr)    # this could be slow

                    related_stm_start_lines = used_name_to_start_lines.get(defined_name, set())
                    _lines = sorted(list(set().union(*[start_line_to_stm_lines.get(___l, set()) for ___l in related_stm_start_lines])))
                    relevant_statements = "".join([file_lines[___l - 1] for ___l in _lines])
                    InstanceLogger().get_logger().info(
                        f"Relevant statements for {defined_name} in {py_path}: \n{relevant_statements}\n\n"
                    )

                    current_idxes = sorted(list(set().union(*[line_2_idx[l] for l in _lines])))
                    dependent_instr = [instructions_raw[__idx] for __idx in current_idxes if idx_2_line[__idx] >= body_starts_line]
                    __checksum = instr2checksum(naive_instr_to_tuple_list(dependent_instr, handling_local=True), pyc_path, f"Variable({defined_name})")

                    _use_names = set().union(*[start_line_to_names.get(sl, set()) for sl in related_stm_start_lines])
                    _use_attrs = set().union(*[start_line_to_attrs.get(sl, set()) for sl in related_stm_start_lines])
                    _use_boths = set().union(*[start_line_to_boths.get(sl, set()) for sl in related_stm_start_lines])

                    # 在一个类中，没有 dot 运算符的还可能是定义在类里面的 member
                    if handle_class:
                        _use_attrs.update(_use_names)
                    _local_variables[defined_name] = {
                        "checksum": __checksum,
                        "external_names": list(_use_names),
                        "external_attributes": list(_use_attrs),
                        "external_boths": list(_use_boths),
                    }
                return _local_variables, external_names, external_attrs, external_boths

            if code_type == "module":
                local_variables = dict()
                injected_fixtures = set()
                external_names, external_attrs, external_boths = set(), set(), set()
                if not os.path.basename(pyc_path).startswith("test_"):
                    local_variables, external_names, external_attrs, external_boths = get_local_variables()
                else:
                    injected_fixtures = TreeSitterClient().get_injected_fixtures(parsed_tree.root_node, file_content)
                build_co = ModuleObject.make_object(code_type_wrapper, pyc_path, local_variables, injected_fixtures, external_names, external_attrs, external_boths)
            elif code_type == "function":
                build_co = FunctionObject.make_object(code_type_wrapper, pyc_path, instructions, class_name, pre_instr, post_instr, decorator_instr_list, body_starts_line)
            elif code_type == "class":
                local_variables, _, _, _ = get_local_variables(handle_class=True)
                build_co = ClassObject.make_object(code_type_wrapper, pyc_path, super_classes, local_variables, decorator_instr_list)
            else:
                raise RuntimeError(f"Unsupported code type: {code_type}")

            co_dict[build_co.name].append(build_co)

        return instructions, build_co, code_type_wrapper

    co_dict = defaultdict(list)
    code_obj = load_code_from_pyc(os.path.join(project_path, pyc_path))
    _, module_object, _ = parse_code_obj(code_obj, "module", [], [], [], -1)

    return module_object, co_dict
