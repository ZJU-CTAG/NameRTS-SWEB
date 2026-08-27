import json
import os
import marshal
import dis
import hashlib
import pprint
import time
import types
import re

import xxhash

from src.bytecode import load_code_from_pyc, canonical_repr, pyc_to_py, get_instr_line
from src.utils import compare_checksums_dict
from src.instance_logger import InstanceLogger
from collections.abc import Set, Mapping, Sequence
from pathlib import Path
from dis import Instruction
from src.utils import Timer, subprocess_run_stdout


def extract_instructions(code_obj):
    instructions = []

    line_mappings = dict()
    idx = 0
    for instr in dis.get_instructions(code_obj):
        line_number = get_instr_line(instr)
        if line_number is not None:
            line_mappings[idx] = line_number
        if instr.opname == "LOAD_CONST" and isinstance(instr.argval, types.CodeType):
            instructions.append((instr.opname, f'<code object {instr.argval.co_name}>'))
            idx += 1

            sub_instr, sub_line_mappings = extract_instructions(instr.argval)
            for i, line_num in sub_line_mappings.items():
                line_mappings[i + idx] = line_num
            instructions.extend(sub_instr)
            idx += len(sub_instr)

            continue
        instructions.append((instr.opname, instr.argval))
        idx += 1

    instructions.append(("ENDDEFINE", code_obj.co_name))
    return instructions, line_mappings


def idx_to_linen_dict(instructions, line_mappings):
    total_len = len(instructions)
    ret = dict()
    last = line_mappings[min(line_mappings.keys())]
    ret[0] = last
    for idx in range(1, total_len):
        if idx in line_mappings:
            last = line_mappings[idx]
        ret[idx] = last
    return ret


def pyc_checksum(pyc_path: str):
    code_obj = load_code_from_pyc(pyc_path)
    if code_obj is None:
        return None

    instructions, _ = extract_instructions(code_obj)
    content = construct_bytecode(instructions)
    # InstanceLogger().get_logger().info(f"Parsed {pyc_path}: \n{content}\n\n\n")
    return xxhash.xxh3_64(content.encode("utf-8")).hexdigest()


def construct_bytecode(instructions):
    return "\n".join(f"{op}:{canonical_repr(val)};" for op, val in instructions)


def gen_checksums_and_compare(newest_checksums: dict, project_path: str, update_subset: set, naive: bool = False):
    current_checksums = dict()
    if naive:
        all_py = list(Path(project_path).rglob("*.py"))
        for py_path in all_py:
            abs_path = str(py_path.resolve())
            rel_path = str(py_path.relative_to(project_path))
            with open(abs_path, "r", errors="ignore") as py_file:
                code = py_file.read()
            # check_sum = hashlib.md5(code.encode("utf-8")).hexdigest()
            check_sum = xxhash.xxh3_64(code.encode("utf-8")).hexdigest()
            current_checksums[rel_path] = check_sum
    else:
        all_pycs = list(Path(project_path).rglob("*.pyc"))
        for pyc_path in all_pycs:
            abs_path = str(pyc_path.resolve())
            rel_path = str(pyc_path.relative_to(project_path))
            if pyc_to_py(rel_path) in update_subset:
                check_sum = pyc_checksum(abs_path)
            else:
                check_sum = newest_checksums.get(rel_path, None)
            if check_sum is not None:
                current_checksums[rel_path] = check_sum
            else:
                InstanceLogger().get_logger().error(f"Failed to compute checksum for {pyc_path}.")
    InstanceLogger().get_logger().info(f"Resulting checksum dict: \n{pprint.pformat(current_checksums)}\n\n\n")

    # compare checksums dicts
    changed_pycs, added, removed = compare_checksums_dict(newest_checksums, current_checksums)
    changed_files = [pyc_to_py(s) for s in changed_pycs]

    InstanceLogger().get_logger().info(f"Changed files: \n{pprint.pformat(changed_files)}\n\n\n")
    return changed_files, current_checksums, added, removed


def get_changed_files(project_path: str, naive = False):
    py_checksums_path = os.path.join(project_path, "py_checksums_cache.json")
    previous_checksums = dict()
    if os.path.exists(py_checksums_path):
        with open(py_checksums_path, "r") as previous_checksums_file:
            previous_checksums = json.load(previous_checksums_file)

    # changed .py files
    previous_checksums_py = {k: v for k, v in previous_checksums.items() if k.endswith(".py")}
    changed_files, current_checksums_py, added, removed = gen_checksums_and_compare(
        newest_checksums=previous_checksums_py,
        project_path=project_path,
        update_subset=set(),
        naive=True
    )
    InstanceLogger().get_logger().info(f"Removed files: \n{pprint.pformat(removed)}\n\n\n")

    # changed bytecode
    current_checksums_pyc = dict()
    if not naive:
        previous_checksums_pyc = {k: v for k, v in previous_checksums.items() if k.endswith(".pyc")}
        changed_files, current_checksums_pyc, _, _ = gen_checksums_and_compare(
            newest_checksums=previous_checksums_pyc,
            project_path=project_path,
            update_subset=set(changed_files),
            naive=False
        )

    # create new checksums cache
    current_checksums = current_checksums_py.copy()
    current_checksums.update(current_checksums_pyc)
    with open(py_checksums_path, "w") as py_checksums_file:
        json.dump(current_checksums, py_checksums_file, indent=4)

    return set(changed_files), added, removed


