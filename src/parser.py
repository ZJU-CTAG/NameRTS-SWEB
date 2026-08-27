import traceback
from collections import defaultdict

from tree_sitter import Language, Parser, Node
import pprint
import logging
from typing import Set

try:
    import tree_sitter_python as tspython
except ImportError:  # Python 3.6 uses the last compatible language wheel.
    tspython = None
    from tree_sitter_languages import get_language

from src.instance_logger import InstanceLogger

query_class_content = """\
(class_definition) @class
"""

query_potential_comments_content = """\
(string) @string
(comment) @comment
"""

query_function_content = """\
(function_definition) @function
"""

query_decorated_definition_content = """\
(decorated_definition) @dd
"""

query_import_content = """\
(import_statement) @is
(import_from_statement) @is
"""

class TreeSitterClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)

            if tspython is not None:
                cls._instance.PY_LANGUAGE = Language(tspython.language())
                cls._instance.parser = Parser(cls._instance.PY_LANGUAGE)
            else:
                cls._instance.PY_LANGUAGE = get_language("python")
                cls._instance.parser = Parser()
                cls._instance.parser.set_language(cls._instance.PY_LANGUAGE)

            # create queries
            cls._instance.query_class = cls._instance.PY_LANGUAGE.query(query_class_content)
            cls._instance.query_function = cls._instance.PY_LANGUAGE.query(query_function_content)
            cls._instance.query_dd = cls._instance.PY_LANGUAGE.query(query_decorated_definition_content)
            cls._instance.query_is = cls._instance.PY_LANGUAGE.query(query_import_content)
            cls._instance.query_potential_comments = cls._instance.PY_LANGUAGE.query(query_potential_comments_content)

        return cls._instance

    @classmethod
    def parse(cls, code_bytes):
        return cls._instance.parser.parse(code_bytes)

    @classmethod
    def _captures(cls, query, root_node):
        captures = query.captures(root_node)
        if isinstance(captures, dict):
            return captures
        normalized = defaultdict(list)
        for node, capture_name in captures:
            normalized[capture_name].append(node)
        return normalized

    @classmethod
    def _capture_query_class(cls, root_node):
        return cls._captures(cls._instance.query_class, root_node)

    @classmethod
    def _capture_query_function(cls, root_node):
        return cls._captures(cls._instance.query_function, root_node)

    @classmethod
    def _capture_query_dd(cls, root_node):
        return cls._captures(cls._instance.query_dd, root_node)

    @classmethod
    def _capture_query_is(cls, root_node):
        return cls._captures(cls._instance.query_is, root_node)

    @classmethod
    def get_dynamic_import_statements(cls, root_node):
        capture_is = cls._capture_query_is(root_node)
        ret = []
        for is_node in capture_is.get("is", []):
            is_inner = False
            parent_node = is_node.parent
            while parent_node:
                if parent_node.type == "function_definition":
                    is_inner = True
                    break
                parent_node = parent_node.parent
            if is_inner:
                ret.append(is_node)
        return ret

    @classmethod
    def get_all_imports(cls, root_node, file_content: str):
        def handle_aliased_import(n):
            alias_node = n.child_by_field_name("alias")
            name_node = n.child_by_field_name("name")
            return cls.retrieve_string_node(file_content, name_node), cls.retrieve_string_node(file_content, alias_node)
        def handle_dotted_name(n):
            return cls.retrieve_string_node(file_content, n)

        capture_is = cls._capture_query_is(root_node)
        ret = []
        for is_node in capture_is.get("is", []):
            if is_node.type == "import_from_statement":
                from_node = is_node.child_by_field_name("module_name")
                from_str = cls.retrieve_string_node(file_content, from_node)
            elif is_node.type == "import_statement":
                from_str = None
            else:
                continue
            for child in is_node.children_by_field_name("name"):
                if child.type == "aliased_import":
                    name_str, alias_str = handle_aliased_import(child)
                    ret.append((from_str, name_str, alias_str))
                elif child.type == "dotted_name":
                    name_str = handle_dotted_name(child)
                    alias_str = None
                    ret.append((from_str, name_str, alias_str))
        return ret

    @classmethod
    def get_decorated_definitions(cls, root_node, dd_line):
        capture_function = cls._capture_query_dd(root_node)
        for dd_node in capture_function.get("dd", []):
            for child in dd_node.children:
                if child.type == "decorator" and child.start_point[0] == dd_line:
                    return dd_node.child_by_field_name("definition")
        return None

    @classmethod
    def _capture_query_potential_comments(cls, root_node):
        return cls._captures(cls._instance.query_potential_comments, root_node)

    @classmethod
    def get_outer_class_nodes(cls, root_node):
        ret = []
        capture_class = cls._capture_query_class(root_node)
        for class_node in capture_class.get("class", []):

            outer_class = True
            current = class_node.parent
            while current is not None:
                if current.type in ["function_definition", "class_definition"]:
                    outer_class = False
                    break
                current = current.parent

            if outer_class:
                if class_node.parent is not None and class_node.parent.type == "decorated_definition":
                    ret.append(class_node.parent)
                else:
                    ret.append(class_node)
        return ret

    @classmethod
    def get_function_nodes(cls, any_node: Node):
        """
        returning exceptionally inner functions nodes
        :param any_node:
        :return:
        """
        all_function_nodes = []
        capture_function = cls._capture_query_function(any_node)
        for function_node in capture_function.get("function", []):
            if function_node.parent is not None and function_node.parent.type == "decorated_definition":
                function_node = function_node.parent
            all_function_nodes.append(function_node)

        # filter out inner functions
        all_function_nodes.sort(key=lambda x: (x.start_point[0], x.start_point[1], -x.end_point[0], -x.end_point[1]))
        outer_functions = []
        for curr in all_function_nodes:
            is_contained = False
            for parent in outer_functions:
                # if current function node is contained by another function node
                if curr.start_point >= parent.start_point and curr.end_point <= parent.end_point:
                    is_contained = True
                    break
            if not is_contained:
                outer_functions.append(curr)
        return outer_functions


    @classmethod
    def get_line_to_func_body_mapping(cls, root_node: Node):
        """
        linenos starts from 1
        :param root_node:
        :return:
        """
        outer_functions = cls.get_function_nodes(root_node)
        ret = dict()
        for function_node in outer_functions:
            function_lines = list(range(function_node.start_point[0] + 1, function_node.end_point[0] + 2))
            try:
                if function_node.type == "function_definition":
                    body_node = function_node.child_by_field_name("body")
                else:
                    body_node = function_node.child_by_field_name("definition").child_by_field_name("body")
                statements = body_node.children
                if len(statements) == 0:
                    continue
                statements = [s for s in statements if s.type != "comment"]

                # abstract method (def name(): ...)
                if statements[0].type == "expression_statement" and \
                        len(statements[0].children) == 1 and \
                        statements[0].children[0].type == "ellipsis":
                    continue

                # doc string
                if statements[0].type == "expression_statement" and \
                    len(statements[0].children) == 1 and \
                    statements[0].children[0].type == "string":
                    if len(statements) == 1:
                        # TODO: can we do better?
                        body_start_line = statements[0].start_point[0] + 1
                    else:
                        body_start_line = statements[1].start_point[0] + 1
                else:
                    body_start_line = statements[0].start_point[0] + 1
                for l in function_lines:
                    ret[l] = body_start_line
            except Exception as e:
                # just in case
                InstanceLogger().get_logger().error(
                    f"Exception in get_line_to_func_body_mapping: {e}\n"
                    f"{traceback.format_exc()}"
                )

        return ret


    @classmethod
    def defines(cls, node: Node):
        def_identifiers = []
        if node is None:
            return def_identifiers
        if node.type == "expression_statement":
            if len(node.children) == 1 and node.children[0].type == "assignment":
                assignment_node = node.children[0]
                left_node = assignment_node.child_by_field_name("left")
                right_node = assignment_node.child_by_field_name("right")
                if right_node is not None:
                    def_identifiers += cls.defines(left_node)
            if len(node.children) == 1 and node.children[0].type == "call":
                call_node = node.children[0]
                object_node = call_node.child_by_field_name("object")
                def_identifiers += cls.defines(object_node)
        elif node.type == "if_statement":
            consequence_node = node.child_by_field_name("consequence")
            for child in consequence_node.children:
                def_identifiers += cls.defines(child)
        elif node.type in ["while_statement", "for_statement"]:
            body_node = node.child_by_field_name("body")
            for child in body_node.children:
                def_identifiers += cls.defines(child)
        elif node.type == "identifier":
            def_identifiers.append(node)
        elif node.type == "subscript":
            value_node = node.child_by_field_name("value")
            def_identifiers += cls.defines(value_node)
        elif node.type == "pattern_list":
            for child in node.children:
                def_identifiers += cls.defines(child)
        return def_identifiers

    @classmethod
    def get_global_variables_statement_nodes(cls, root_node: Node):
        ret = []
        def dfs(node: Node):
            node_type = node.type
            if node_type in ["class_definition", "decorated_definition", "function_definition", "comment"] or \
                any((s in node_type) for s in ["import"]):
                return
            elif node_type in ["expression_statement", "if_statement", "for_statement", "while_statement"]:
                defs = cls.defines(node)
                if defs:
                    ret.append((node, defs))
            else:
                for child in node.children:
                    dfs(child)

        dfs(root_node)
        return ret

    @classmethod
    def get_class_static_variables_statement_nodes(cls, root_node: Node):
        ret = []
        capture_class = cls._capture_query_class(root_node)
        for class_node in capture_class.get("class", []):
            body_node = class_node.child_by_field_name("body")
            for child in body_node.children:
                if child.type in ["expression_statement", "if_statement", "for_statement", "while_statement"]:
                    defs = cls.defines(child)
                    ret.append((child, defs))
        return ret

    @classmethod
    def get_all_statements(cls, root_node: Node):
        ret = []
        def dfs(node: Node):
            node_type = node.type
            if node_type.endswith("statement"):
                if "import" not in node_type:
                    ret.append(node)
            else:
                for child in node.children:
                    dfs(child)

        dfs(root_node)
        ret.reverse()
        return ret

    @classmethod
    def is_in_a_function(cls, node: Node):
        while node.parent is not None:
            node = node.parent
            if node.type == "function_definition":
                return True
        return False

    @classmethod
    def get_field(cls, field_name, any_node, code_str):
        name_identifier_node = any_node.child_by_field_name(field_name)
        if name_identifier_node is None and any_node.type == "decorated_definition":
            definition_node = any_node.child_by_field_name("definition")
            if definition_node is not None:
                name_identifier_node = definition_node.child_by_field_name(field_name)
        if name_identifier_node is None:
            InstanceLogger().get_logger().error(f"Field {field_name} not found for \n{cls.retrieve_string_node(code_str, any_node)}\n")
            return ""
        return cls.retrieve_string_node(code_str, name_identifier_node)

    @classmethod
    def get_line_list(cls, code_str, start_line, end_line):
        lines = code_str.splitlines(keepends=True)
        lines = lines[start_line:end_line + 1]
        return lines

    @classmethod
    def retrieve_string_slice(cls, code_str: str, start_point, end_point):
        string_list = cls.get_line_list(code_str, start_point[0], end_point[0])
        string_list[-1] = string_list[-1][:end_point[1]]
        string_list[0] = string_list[0][start_point[1]:]
        return "".join(string_list)

    @classmethod
    def retrieve_string_node(cls, code_str: str, node):
        return cls.retrieve_string_slice(code_str, node.start_point, node.end_point)

    @classmethod
    def retrieve_string_node_lines(cls, code_str: str, node):
        return "".join(cls.get_line_list(code_str, node.start_point[0], node.end_point[0]))

    @classmethod
    def display_ast(cls, root, func, depth=0):
        print(" - " * depth, root.type)
        pprint.pprint(cls.retrieve_string_slice(func, root.start_point, root.end_point))
        print("-" * 100)
        for child in root.children:
            cls.display_ast(child, func, depth + 1)

    @classmethod
    def same_position(cls, node_1, node_2):
        return node_1.start_point == node_2.start_point and node_1.end_point == node_2.end_point

    @classmethod
    def get_functions_by_l(cls, file_contents: str, lines: Set[int]):
        """
        extract function names based on code lines
        line number starts from 1
        :param file_contents:
        :param lines:
        :return:
        """
        ret = []
        parsed_tree = cls._instance.parser.parse(file_contents.encode("utf-8"))
        outer_function_nodes = cls.get_function_nodes(parsed_tree.root_node)
        for function_node in outer_function_nodes:
            start_line = function_node.start_point[0] + 1
            end_line = function_node.end_point[0] + 1
            if any(start_line <= l <= end_line for l in lines):
                func_name = cls.get_func_fullname(file_contents, function_node)
                if func_name.endswith("?"):
                    InstanceLogger().get_logger().error(
                        f"Failed to retrieve function name: \n\n{cls.retrieve_string_node(file_contents, function_node)}\n\n")
                else:
                    ret.append((func_name, function_node.start_point, function_node.end_point))
        return ret

    @classmethod
    def get_func_fullname(cls, file_contents: str, function_node: Node):
        outer_classes = cls.get_outer_class(file_contents, function_node)
        func_name = cls.get_field("name", function_node, file_contents)
        if not func_name:
            func_name = "?"
        if len(outer_classes) != 0:
            func_name = ".".join(outer_classes) + "::" + func_name
        return func_name

    @classmethod
    def get_function_fullname_by_l(cls, file_contents: str, line: int):
        funcs = cls.get_functions_by_l(file_contents, {line})
        if not len(funcs):
            InstanceLogger().get_logger().error(f"Failed to retrieve function name at line {line}: \n\n{file_contents}\n\n")
            return ""
        return funcs[0][0]

    @classmethod
    def get_functions_fullname_by_l(cls, file_contents: str, lines: Set[int]):
        funcs = cls.get_functions_by_l(file_contents, lines)
        return [func[0] for func in funcs]

    @classmethod
    def get_outer_class(cls, file_contents: str, node: Node):
        ret = []
        current = node.parent
        while current is not None:
            if current.type == "class_definition":
                ret.append(cls.get_field("name", current, file_contents))
            current = current.parent
        ret.reverse()
        return ret

    @classmethod
    def get_function_node_by_name(cls, file_contents: str, target_function_name: str):
        parsed_tree = cls._instance.parser.parse(file_contents.encode("utf-8"))
        outer_function_nodes = cls.get_function_nodes(parsed_tree.root_node)
        for function_node in outer_function_nodes:
            function_name = cls.get_func_fullname(file_contents, function_node)
            if function_name == target_function_name:
                return function_node
        return None

    @classmethod
    def remove_comments(cls, file_contents: str):
        code_bytes = file_contents.encode("utf-8")
        parsed_tree = cls._instance.parser.parse(code_bytes)
        to_remove = []

        capture_comments = cls._capture_query_potential_comments(parsed_tree.root_node)
        for string_node in capture_comments.get("string", []):
            if string_node.parent is not None and string_node.parent.type == "expression_statement":
                to_remove.append((string_node.start_byte, string_node.end_byte))
        for comment_node in capture_comments.get("comment", []):
            to_remove.append((comment_node.start_byte, comment_node.end_byte))

        to_remove = sorted(to_remove, key=lambda x: x[0], reverse=True)
        code_bytearray = bytearray(code_bytes)

        for start_byte, end_byte in to_remove:
            code_bytearray[start_byte:end_byte] = b''

        return bytes(code_bytearray).decode()

    @classmethod
    def closest_func_by_name(cls, file_contents: str, line: int, target_function_name: str, function_namedict: dict):
        line -= 1   # start from 0
        if target_function_name not in function_namedict:
            # InstanceLogger().get_logger().warning(
            #     f"Failed to find function with name {target_function_name} in {filepath}")
            return target_function_name

        # 如果对应名字只有一个，直接返回（但是如果有多个类，可能存在同名函数！）
        if len(function_namedict[target_function_name]) == 1:
            target_func_node = list(function_namedict[target_function_name])[0]
        else:
            # （heuristic）如果对应名字找到了多个 func_node：首先判断是否存在包含关系，没有则找离边界最近的
            func_in = [f for f in function_namedict[target_function_name] if line in range(f.start_point[0], f.end_point[0] + 1)]
            if len(func_in):
                target_func_node = func_in[0]
            else:
                func_near = [(line - f.end_point[0] if line > f.end_point[0] else f.start_point[0] - line, f)
                             for f in function_namedict[target_function_name]]
                target_func_node = sorted(func_near, key=lambda x: x[0])[0][1]

        return cls.get_func_fullname(file_contents, target_func_node)

    @classmethod
    def get_sub_funcs(cls, file_contents: str, func_node: Node) -> Set[str]:
        ret = set()
        capture_function = cls._capture_query_function(func_node)
        for sub_func_node in capture_function.get("function", []):
            func_name = cls.get_field("name", sub_func_node, file_contents)
            if func_name:
                ret.add(func_name)
        return ret

    @classmethod
    def get_injected_fixtures(cls, test_file_root_node: Node, file_contents: str):
        ret = set()
        func_nodes = cls.get_function_nodes(test_file_root_node)
        for func_node in func_nodes:
            can_inject = False

            if func_node.type == "decorated_definition":
                for child in func_node.children:
                    if child.type == "decorator":
                        decorator_str = cls.retrieve_string_node(file_contents, child)
                        if "pytest.fixture" in decorator_str:
                            can_inject = True

                func_node = func_node.child_by_field_name("definition")

            func_name = str(cls.get_field("name", func_node, file_contents))
            if func_name.startswith("test_"):
                can_inject = True

            if not can_inject:
                continue
            parameters_node = func_node.child_by_field_name("parameters")
            for child in parameters_node.children:
                if child.type == "identifier":
                    ret.add(cls.retrieve_string_node(file_contents, child))
                elif child.type == "typed_parameter":
                    ret.add(cls.retrieve_string_node(file_contents, child.children[0]))
        return ret

TreeSitterClient()
