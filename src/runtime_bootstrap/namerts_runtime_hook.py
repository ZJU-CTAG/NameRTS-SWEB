"""Framework-neutral runtime dependency capture for NameRTS.

The hook is loaded by ``sitecustomize`` in a process that runs exactly one test
file.  All events observed by that process can therefore be attributed to the
original test-file path supplied by the parent runner.  No pytest hooks or
changes to the target repository are required.

This module intentionally uses syntax and APIs available in Python 3.6.
"""

import atexit
import builtins
import importlib
import json
import os
import sys
import time


_INSTALLED = False
_FINISHED = False
_PROJECT_ROOT = None
_OUT_DIR = None
_TEST_FILE = None
_NBDP = None
_EVENTS = set()

_ORIGINAL_IMPORT = builtins.__import__
_ORIGINAL_IMPORT_MODULE = importlib.import_module


def _inside_project(path):
    if not path or not _PROJECT_ROOT:
        return False
    try:
        absolute = os.path.abspath(path)
    except Exception:
        return False
    return absolute == _PROJECT_ROOT or absolute.startswith(
        _PROJECT_ROOT + os.sep
    )


def _patched_import(name, globals=None, locals=None, fromlist=None, level=0):
    if _NBDP is not None:
        _NBDP.is_importing += 1
    try:
        return _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
    finally:
        if _NBDP is not None:
            _NBDP.is_importing -= 1


def _patched_import_module(name, package=None):
    importer = None
    lineno = None
    try:
        caller_frame = sys._getframe(1)
        importer = caller_frame.f_code.co_filename
        lineno = caller_frame.f_lineno
    except Exception:
        pass
    finally:
        try:
            del caller_frame
        except Exception:
            pass

    importing = _inside_project(importer)
    if importing and _NBDP is not None:
        _NBDP.is_importing += 1
    try:
        result = _ORIGINAL_IMPORT_MODULE(name, package)
    finally:
        if importing and _NBDP is not None:
            _NBDP.is_importing -= 1

    imported = None
    try:
        imported = getattr(result, "__file__", None)
    except Exception:
        pass
    if importer and imported and (importing or _inside_project(imported)):
        _EVENTS.add((importer, imported, lineno))
    return result


def _write_events(prefix, events):
    try:
        os.makedirs(_OUT_DIR)
    except OSError:
        if not os.path.isdir(_OUT_DIR):
            return
    suffix = "{:.9f}.{}".format(time.time(), os.getpid())
    path = os.path.join(_OUT_DIR, "{}.json.{}".format(prefix, suffix))
    try:
        with open(path, "w", encoding="utf-8") as output:
            json.dump(
                list(events),
                output,
                indent=2,
                ensure_ascii=False,
            )
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        # Capture must remain observational: a write failure cannot turn a
        # passing target test into a failure.
        pass


def _finish():
    global _FINISHED
    if _FINISHED:
        return
    _FINISHED = True

    # Restore first so serialization and other atexit handlers do not add
    # events or exercise the instrumented builtins.
    builtins.__import__ = _ORIGINAL_IMPORT
    importlib.import_module = _ORIGINAL_IMPORT_MODULE

    test_events = set(_EVENTS)
    import_time_events = set()
    if _NBDP is not None:
        try:
            test_events.update(
                (name, None, None) for name in _NBDP.get_all_visited()
            )
        except Exception:
            pass
        try:
            import_time_events.update(
                (name, None, None)
                for name in _NBDP.visited_when_importing
            )
        except Exception:
            pass
        try:
            test_events.update(
                ("captured_getattr_name", name, None)
                for name in _NBDP.get_captured_getattr_names()
            )
        except Exception:
            pass
        try:
            test_events.update(
                ("captured_member_name", name, None)
                for name in _NBDP.get_captured_member_names()
            )
        except Exception:
            pass

    encoded_test_file = _TEST_FILE.replace("/", "..").replace(os.sep, "..")
    _write_events(encoded_test_file, test_events)
    if import_time_events:
        _write_events("import_pairs", import_time_events)


def install_from_environment():
    """Install capture when the parent runner explicitly enables it."""
    global _INSTALLED
    global _PROJECT_ROOT
    global _OUT_DIR
    global _TEST_FILE
    global _NBDP

    if _INSTALLED or os.environ.get("NAMERTS_CAPTURE_DEPENDENCIES") != "1":
        return

    project_root = os.environ.get("NAMERTS_PROJECT_ROOT")
    out_dir = os.environ.get("NAMERTS_COVERAGE_DIR")
    test_file = os.environ.get("NAMERTS_TEST_FILE")
    if not project_root or not out_dir or not test_file:
        return

    _PROJECT_ROOT = os.path.abspath(project_root)
    _OUT_DIR = os.path.abspath(out_dir)
    _TEST_FILE = test_file.replace("\\", "/")
    if os.environ.get("NAMERTS_NBDP_CAPTURING") == "1":
        try:
            import nbdp_instrumentor
            _NBDP = nbdp_instrumentor
            _NBDP.collected_started = True
        except Exception:
            _NBDP = None

    builtins.__import__ = _patched_import
    importlib.import_module = _patched_import_module
    atexit.register(_finish)
    _INSTALLED = True
