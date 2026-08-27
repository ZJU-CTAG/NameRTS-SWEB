import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOS_ROOT = "/shared_dir/repos"
RUNTIME_ROOT = os.path.join(PROJECT_ROOT, "runtime")
GROUND_TRUTH_PATH = os.path.join(PROJECT_ROOT, "ground truth")

REGISTRY_DECORATOR_KEYWORDS = set()
DIVIDE_DYNAMIC_IMPORTS = False

def get_test_command(repo_path):
    if os.path.basename(os.path.normpath(repo_path)) == "django":
        # NameBDP may insert monitor initialization before runtests.py's
        # shebang. Invoke it through the target environment's interpreter so
        # the instrumented file is never mistaken for a shell script.
        return "python ./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"
    if "prefect" in repo_path:
        return "env -u ALL_PROXY pytest"
    elif "pandas" in repo_path:
        return "env NUMEXPR_MAX_THREADS=400 pytest -m \"not single_cpu\""
    elif "seaborn" in repo_path:
        return "pytest -k \"not test_load_datasets and not test_load_cached_datasets\""
    elif "sphinx" in repo_path:
        return "pytest -k \"not test_build_linkcheck\""

    return "pytest"


def get_test_target(repo_path, test_file):
    """Translate a repository-relative test file to the runner's argument."""
    if os.path.basename(os.path.normpath(repo_path)) != "django":
        return test_file

    normalized = test_file.replace("\\", "/")
    if normalized.startswith("tests/"):
        normalized = normalized[len("tests/"):]
    if normalized.endswith(".py"):
        normalized = normalized[:-len(".py")]
    return normalized.replace("/", ".")

OPERATOR_OVERLOADING_CATEGORIES = {
    "arithmetic_bitwise": {
        "__add__", "__radd__", "__iadd__",
        "__sub__", "__rsub__", "__isub__",
        "__mul__", "__rmul__", "__imul__",
        "__matmul__", "__rmatmul__", "__imatmul__",
        "__truediv__", "__rtruediv__", "__itruediv__",
        "__floordiv__", "__rfloordiv__", "__ifloordiv__",
        "__mod__", "__rmod__", "__imod__",
        "__divmod__", "__rdivmod__",
        "__pow__", "__rpow__", "__ipow__",
        "__lshift__", "__rlshift__", "__ilshift__",
        "__rshift__", "__rrshift__", "__irshift__",
        "__and__", "__rand__", "__iand__",
        "__xor__", "__rxor__", "__ixor__",
        "__or__", "__ror__", "__ior__",
        "__neg__", "__pos__", "__abs__", "__invert__",
    },
    "comparison": {"__lt__", "__le__", "__eq__", "__ne__", "__gt__", "__ge__"},
    "containers_sequence": {
        "__len__", "__length_hint__", "__contains__",
        "__getitem__", "__setitem__", "__delitem__",
        "__iter__", "__next__", "__reversed__",
        "__missing__", "__array__"
    },
    "attribute_access_descriptor": {
        "__getattr__", "__getattribute__", "__setattr__", "__delattr__",
        "__get__", "__set__", "__delete__", "__set_name__",
        "__dir__", "__slots__", "__class_getitem__",  # PEP 560 / typing
    },
    "callable_context": {
        "__call__", "__enter__", "__exit__",
        "__aenter__", "__aexit__", "__await__", "__aiter__", "__anext__",
    },
    "conversion_indexing": {
        "__bool__", "__index__", "__int__", "__float__", "__complex__",
        "__bytes__", "__round__",
    },
    "representation_formatting": {
        "__repr__", "__str__", "__format__", "__hash__",
    },
    "pickling_copying": {"__reduce__", "__reduce_ex__", "__copy__", "__deepcopy__"},
    # Keep lifecycle/meta separate so we can exclude them if desired
    "lifecycle_meta": {
        "__new__", "__init__", "__init_subclass__", "__subclasshook__", "__del__",
        "__instancecheck__", "__subclasscheck__",
    },
    "lib_protocol": {
        "__get_pydantic_core_schema__", "__array_ufunc__"
    }
}
OPERATOR_OVERLOADING_EXCLUDE = {"__all__"}
OPERATOR_OVERLOADING_ALL = set.union(*OPERATOR_OVERLOADING_CATEGORIES.values()) - OPERATOR_OVERLOADING_EXCLUDE

FAST_DEBUG = False
INSTRUMENTATION_BLACKLIST = {
    # functions with @jit, can not be instrumented
    "pvlib-python": {
        "spa.py"
    }
}

SELECT_USE_PARALLEL = False
PRUNE_CRITICAL_FUNCTIONS = True
PRUNE_NAME_RESOLUTION = True
NUM_DYNAMIC_MONITOR = 500
# A single historical test module must not block an entire per-file batch.
# SymPy 1.0 has a few modules that exceed ten minutes under Python 3.9.
PER_FILE_TEST_TIMEOUT = 900


DATASET_FILE_NAME = {
    "sympy":    "commit_pairs_sympy.json",
    "sklearn":  "commit_pairs_scikit-learn.json",
    "mpl":      "commit_pairs_matplotlib.json",
    "dask":     "commit_pairs_dask.json",
    "xarray":   "commit_pairs_xarray.json",
    "sphinx":   "commit_pairs_sphinx.json",
    "pylint":   "commit_pairs_pylint.json",
    "seaborn":  "commit_pairs_seaborn.json",
    "pvlib":    "commit_pairs_pvlib.json",
    "loguru":   "commit_pairs_loguru.json",
}

GROUND_TRUTH_FILE_NAME = {
    "sympy":    "gt_sympy.json",
    "sklearn":  "gt_sklearn.json",
    "mpl":      "gt_matplotlib.json",
    "dask":     "gt_dask.json",
    "xarray":   "gt_xarray.json",
    "sphinx":   "gt_sphinx.json",
    "pylint":   "gt_pylint.json",
    "seaborn":  "gt_seaborn.json",
    "pvlib":    "gt_pvlib.json",
    "loguru":   "gt_loguru.json",
}

# Bounded outer-level worker counts for one-test-file processes.
ISOLATION_MULTIPROC_N = {
    "sympy":    50,
    "sklearn":  40,
    "mpl":      40,
    "dask":     40,
    "xarray":   40,
    "sphinx":   30,
    "pylint":   1,
    "seaborn":  30,
    "pvlib":    30,
    "loguru":   40,
}

REGISTRY_DECORATOR_KEYWORDS_REPO = {
    "sympy":    {"register"},
    "sklearn":  set(),
    "mpl":      {"export"},
    "dask":     {"register"},
    "xarray":   set(),
    "sphinx":   set(),
    "pylint":   set(),
    "seaborn":  set(),
    "pvlib":    set(),
    "loguru":   set(),
}

ENV_NAME = {
    "sympy":    "RTSTest_SY",
    "sklearn":  "RTSTest_SC",
    "mpl":      "RTSTest_MA",
    "dask":     "RTSTest_DAS",
    "xarray":   "RTSTest_XA",
    "sphinx":   "RTSTest_SPH",
    "pylint":   "RTSTest_PYL",
    "seaborn":  "RTSTest_SE",
    "pvlib":    "RTSTest_PVL",
    "loguru":   "RTSTest_LOG",
}

RESULTS_PATH = {
    "sympy":    "",
    "sklearn":  "",
    "mpl":      "",
    "dask":     "",
    "xarray":   "",
    "sphinx":   "",
    "pylint":   "",
    "seaborn":  "",
    "pvlib":    "",
    "loguru":   "",
}

TARGET_REPO_PATH = {
    "sympy":    os.path.join(REPOS_ROOT, "sympy"),
    "sklearn":  os.path.join(REPOS_ROOT, "scikit-learn"),
    "mpl":      os.path.join(REPOS_ROOT, "matplotlib"),
    "dask":     os.path.join(REPOS_ROOT, "dask"),
    "xarray":   os.path.join(REPOS_ROOT, "xarray"),
    "sphinx":   os.path.join(REPOS_ROOT, "sphinx"),
    "pylint":   os.path.join(REPOS_ROOT, "pylint"),
    "seaborn":  os.path.join(REPOS_ROOT, "seaborn"),
    "pvlib":    os.path.join(REPOS_ROOT, "pvlib-python"),
    "loguru":   os.path.join(REPOS_ROOT, "loguru"),
}

CACHE_PATH = os.path.join(RUNTIME_ROOT, "cache")
LOGGING = os.path.join(RUNTIME_ROOT, "logs")
RESULTS = os.path.join(RUNTIME_ROOT, "results", "rq1")
