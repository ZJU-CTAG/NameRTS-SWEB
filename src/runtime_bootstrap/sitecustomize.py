"""Automatically install NameRTS dependency capture in a test process.

Python imports ``sitecustomize`` while initializing the interpreter when this
directory is present on ``PYTHONPATH``.  Keeping this tiny entry point separate
from the implementation makes the bootstrap independent of the test framework.
"""

try:
    from namerts_runtime_hook import install_from_environment
except Exception:
    # sitecustomize must never make a test process unstartable.  The framework
    # will notice a missing coverage file and retain the test's exit status.
    install_from_environment = None

if install_from_environment is not None:
    try:
        install_from_environment()
    except Exception:
        pass
