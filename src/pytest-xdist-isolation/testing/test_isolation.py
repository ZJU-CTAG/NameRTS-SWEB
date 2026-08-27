from __future__ import annotations

import os
import json
from pathlib import Path

import pytest


def test_isolates_module_state(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
def pytest_collection_modifyitems(session, config, items):
    items.sort(key=lambda item: item.nodeid)
"""
    )

    pytester.makepyfile(
        shared_state="""value = 0
""",
        test_isolation_a="""import shared_state


def test_modifies_state() -> None:
    shared_state.value = 1
    assert shared_state.value == 1
""",
        test_isolation_b="""import shared_state


def test_state_is_reset_between_files() -> None:
    assert shared_state.value == 0
""",
    )

    result = pytester.runpytest(
        "-n",
        "1",
        "--dist=isolation",
        "test_isolation_a.py",
        "test_isolation_b.py",
    )
    result.assert_outcomes(passed=2)
    assert result.ret == 0

    failing = pytester.runpytest(
        "-n",
        "1",
        "--dist=loadfile",
        "test_isolation_a.py",
        "test_isolation_b.py",
    )
    failing.assert_outcomes(passed=1, failed=1)
    assert failing.ret == 1


def test_isolation_uses_fresh_workers_per_file(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_worker_alpha="""import os
from pathlib import Path


def test_alpha() -> None:
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""), encoding="utf-8"
    )
""",
        test_worker_beta="""import os
from pathlib import Path


def test_beta() -> None:
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""), encoding="utf-8"
    )
""",
        test_worker_gamma="""import os
from pathlib import Path


def test_gamma() -> None:
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""), encoding="utf-8"
    )
""",
    )

    result = pytester.runpytest(
        "-n",
        "1",
        "--dist=isolation",
        "-s",
        "test_worker_alpha.py",
        "test_worker_beta.py",
        "test_worker_gamma.py",
    )
    result.assert_outcomes(passed=3)
    assert result.ret == 0

    worker_ids = {
        (pytester.path / name).with_suffix(".worker").read_text(encoding="utf-8")
        for name in ("test_worker_alpha.py", "test_worker_beta.py", "test_worker_gamma.py")
    }
    assert "" not in worker_ids
    assert len(worker_ids) == 3


def test_isolation_handles_multiple_files_with_warmup(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import json
from pathlib import Path

ready_log = []
start_log = []

def pytest_testnodeready(node):
    ready_log.append(node.gateway.id)

def pytest_runtest_logstart(nodeid, location):
    if nodeid.endswith("::test_state"):
        start_log.append((nodeid, len(ready_log)))

def pytest_sessionfinish(session):
    Path("session_meta.json").write_text(
        json.dumps({"ready": ready_log, "starts": start_log}),
        encoding="utf-8",
    )
"""
    )

    pytester.makepyfile(
        shared_state="""value = 0
""",
        test_isolation_a="""import shared_state
from pathlib import Path
import os


def test_state() -> None:
    assert shared_state.value == 0
    shared_state.value = 1
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""),
        encoding="utf-8",
    )
""",
        test_isolation_b="""import shared_state
from pathlib import Path
import os


def test_state() -> None:
    assert shared_state.value == 0
    shared_state.value = 2
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""),
        encoding="utf-8",
    )
""",
        test_isolation_c="""import shared_state
from pathlib import Path
import os


def test_state() -> None:
    assert shared_state.value == 0
    shared_state.value = 3
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""),
        encoding="utf-8",
    )
""",
        test_isolation_d="""import shared_state
from pathlib import Path
import os


def test_state() -> None:
    assert shared_state.value == 0
    shared_state.value = 4
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""),
        encoding="utf-8",
    )
""",
    )

    result = pytester.runpytest(
        "-n",
        "4",
        "--dist=isolation",
        "-s",
        "test_isolation_a.py",
        "test_isolation_b.py",
        "test_isolation_c.py",
        "test_isolation_d.py",
    )
    result.assert_outcomes(passed=4)

    meta = json.loads(
        Path(pytester.path / "session_meta.json").read_text(encoding="utf-8")
    )
    ready_log = meta["ready"]
    start_log = meta["starts"]

    # Ensure each file started with a fresh worker that had already been announced.
    assert len(start_log) == 4
    for _nodeid, ready_count in start_log:
        assert ready_count >= 2

    # Each file should have run on a unique worker process.
    worker_ids = {
        (pytester.path / name).with_suffix(".worker").read_text(encoding="utf-8")
        for name in (
            "test_isolation_a.py",
            "test_isolation_b.py",
            "test_isolation_c.py",
            "test_isolation_d.py",
        )
    }
    assert "" not in worker_ids
    assert len(worker_ids) == 4

    # Ready log should contain at least one entry per unique worker.
    assert len(ready_log) >= len(worker_ids)
    assert len(set(ready_log)) == len(ready_log)


def test_isolation_recovers_from_worker_crash(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
from pathlib import Path

def pytest_testnodeready(node):
    ready_log = Path("ready.log")
    ready_log.write_text(
        ready_log.read_text(encoding="utf-8") + node.gateway.id + "\\n"
        if ready_log.exists()
        else node.gateway.id + "\\n",
        encoding="utf-8",
    )
"""
    )

    pytester.makepyfile(
        shared_state="""value = 0
""",
        test_crashing="""import os


def test_crash() -> None:
        if not Path("crash_once.flag").exists():
            Path("crash_once.flag").write_text("done", encoding="utf-8")
            os._exit(1)
""",
        test_isolation_ok="""import shared_state
from pathlib import Path
import os


def test_state_reset() -> None:
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""),
        encoding="utf-8",
    )
    assert shared_state.value == 0
    shared_state.value = 12
""",
        test_isolation_ok2="""import shared_state
from pathlib import Path
import os


def test_state_reset_again() -> None:
    Path(__file__).with_suffix(".worker").write_text(
        os.environ.get("PYTEST_XDIST_WORKER", ""),
        encoding="utf-8",
    )
    assert shared_state.value == 0
    shared_state.value = 34
""",
    )

    result = pytester.runpytest(
        "-n",
        "1",
        "--dist=isolation",
        "-s",
        "-rA",
        "test_crashing.py",
        "test_isolation_ok.py",
        "test_isolation_ok2.py",
    )

    # First file should fail, others must still pass.
    result.assert_outcomes(passed=2, failed=1)

    worker_ids = {
        (pytester.path / name).with_suffix(".worker").read_text(encoding="utf-8")
        for name in ("test_isolation_ok.py", "test_isolation_ok2.py")
    }
    assert "" not in worker_ids
    assert len(worker_ids) == 2

    ready_log_path = pytester.path / "ready.log"
    assert ready_log_path.exists()
    ready_entries = ready_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(ready_entries) >= 3


def test_isolation_worker_pool_replenishment(pytester: pytest.Pytester) -> None:
    parameter_n = 5
    num_files = 10
    pytester.makeconftest(
        """
import json
from pathlib import Path

def pytest_testnodeready(node):
    path = Path("ready_workers.json")
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = []
    data.append(node.gateway.id)
    path.write_text(json.dumps(data), encoding="utf-8")
"""
    )

    files = {
        f"test_auto_{idx}.py": "def test_ok() -> None:\n    assert True\n"
        for idx in range(num_files)
    }
    pytester.makepyfile(**files)

    result = pytester.runpytest(
        "-n",
        f"{parameter_n}",
        "--dist=isolation",
        "-s",
        *files.keys(),
    )
    result.assert_outcomes(passed=num_files)

    data_path = pytester.path / "ready_workers.json"
    assert data_path.exists()
    worker_ids = json.loads(data_path.read_text(encoding="utf-8"))
    unique_workers = set(worker_ids)

    assert len(unique_workers) >= parameter_n + 1
    assert len(unique_workers) <= num_files + (parameter_n - 1)


def test_isolation_executes_tests_serially(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import json
import os
import time
from pathlib import Path

import pytest

def _append(event: dict) -> None:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "unknown")
    directory = Path("timeline")
    directory.mkdir(exist_ok=True)
    path = directory / f"{worker}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\\n")

@pytest.fixture(autouse=True)
def ensure_serial(request):
    lock = Path("running.lock")
    if lock.exists():
        raise AssertionError(
            f"Concurrent execution detected while running {request.node.nodeid}, "
            f"lock held by {lock.read_text(encoding='utf-8')}"
        )
    lock.write_text(request.node.nodeid, encoding="utf-8")
    try:
        yield
    finally:
        if lock.exists():
            lock.unlink()

def pytest_runtest_logstart(nodeid, location):
    _append(
        {
            "event": "start",
            "nodeid": nodeid,
            "time": time.time(),
            "worker": os.environ.get("PYTEST_XDIST_WORKER", "unknown"),
        }
    )

def pytest_runtest_logfinish(nodeid, location):
    _append(
        {
            "event": "finish",
            "nodeid": nodeid,
            "time": time.time(),
            "worker": os.environ.get("PYTEST_XDIST_WORKER", "unknown"),
        }
    )
"""
    )

    files = {}
    for idx in range(6):
        files[f"test_serial_{idx}.py"] = (
            "import time\n\n"
            "def test_sleep() -> None:\n"
            "    time.sleep(0.1)\n"
        )
    pytester.makepyfile(**files)

    result = pytester.runpytest(
        "-n",
        "3",
        "--dist=isolation",
        "-s",
        *files.keys(),
    )
    result.assert_outcomes(passed=6)

    timeline_dir = pytester.path / "timeline"
    assert timeline_dir.exists()

    events: list[dict[str, object]] = []
    for path in sorted(timeline_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                event = json.loads(line)
                if event["worker"] == "unknown":
                    continue
                events.append(event)
    events.sort(key=lambda entry: entry["time"])

    start_events = [e for e in events if e["event"] == "start"]
    finish_events = [e for e in events if e["event"] == "finish"]
    assert len(start_events) == len(files)
    assert len(finish_events) == len(files)

    start_times: dict[str, float] = {}
    for event in start_events:
        start_times[event["nodeid"]] = event["time"]

    for event in finish_events:
        nodeid = event["nodeid"]
        assert nodeid in start_times, f"Finish for {nodeid} without start entry"
        assert event["time"] >= start_times[nodeid]


def test_isolation_does_not_wait_for_full_pool(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import json
from pathlib import Path

ready_log = []
start_ready_counts = []


def pytest_testnodeready(node):
    ready_log.append(node.gateway.id)


def pytest_runtest_logstart(nodeid, location):
    start_ready_counts.append(len(ready_log))


def pytest_sessionfinish(session):
    Path("isolation_ready.json").write_text(
        json.dumps({"ready": ready_log, "starts": start_ready_counts}),
        encoding="utf-8",
    )
"""
    )

    files = {
        f"test_pool_{idx}.py": "def test_ok() -> None:\n    assert True\n"
        for idx in range(4)
    }
    pytester.makepyfile(**files)

    parameter_n = 6

    result = pytester.runpytest(
        "-n",
        f"{parameter_n}",
        "--dist=isolation",
        *files.keys(),
    )
    result.assert_outcomes(passed=len(files))

    data_path = pytester.path / "isolation_ready.json"
    assert data_path.exists()
    metrics = json.loads(data_path.read_text(encoding="utf-8"))
    assert metrics["starts"], "expected runtest events to be recorded"

    first_start_ready = metrics["starts"][0]
    assert 1 <= first_start_ready < parameter_n
