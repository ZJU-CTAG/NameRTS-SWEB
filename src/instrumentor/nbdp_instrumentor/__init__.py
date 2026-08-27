

is_importing = 0
visited_when_importing = set()
_GLOBALS = globals()
collected_started = False
captured_getattr_names = set()
captured_member_names = set()

def reset_all():
    pass
    # for k in list(_GLOBALS.keys()):
    #     if k.startswith("critical_function_") or k.startswith("decorator_checksum_"):
    #         _GLOBALS[k] = False


def get_all_visited():
    visited = set()
    for k, v in _GLOBALS.items():
        if (k.startswith("critical_function_") or k.startswith("decorator_checksum_")) and v:
            visited.add(k)
            _GLOBALS[k] = False
    return visited


def capture_getattr_name(name):
    """Record and return a getattr name without wrapping getattr itself."""
    if isinstance(name, str):
        captured_getattr_names.add(name)
    return name


def capture_getmembers_result(members):
    """Record and return an inspect.getmembers-style result."""
    try:
        for item in members:
            if (
                isinstance(item, tuple)
                and item
                and isinstance(item[0], str)
            ):
                captured_member_names.add(item[0])
    except Exception:
        pass
    return members


def get_captured_getattr_names():
    names = set(captured_getattr_names)
    captured_getattr_names.clear()
    return names


def get_captured_member_names():
    names = set(captured_member_names)
    captured_member_names.clear()
    return names



# def visit(visit_check: bool, visit_check_import: bool, visit_check_name):
#     if is_importing:
#         if not visit_check_import:
#             _GLOBALS["import_" + visit_check_name] = True
#             visited_when_importing.add(visit_check_name)
#     elif not visit_check:
#         _GLOBALS[visit_check_name] = True




