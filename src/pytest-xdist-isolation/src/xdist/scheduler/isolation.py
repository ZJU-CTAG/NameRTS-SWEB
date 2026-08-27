from __future__ import annotations

"""
Pre-spawned per-file scheduler.

This module implements ``--dist=isolation`` for pytest-xdist. The goal is to
provide strict process isolation between test modules while still hiding the
worker start-up cost.  Achieving that requires a controller-driven pipeline that
keeps one worker executing, another pre-collected and ready, and any additional
workers waiting in a warm standby pool.

Key behaviours:

* Isolation - Every test file is executed in its own worker process.  As soon as
  a worker receives a file, the controller immediately sends a shutdown command,
  guaranteeing the process is torn down after that file finishes.
* Pre-spawn / warm-up - The controller always keeps a spare worker that has
  already completed collection.  When the active worker finishes, the next one
  can start running the very next moment, while the controller simultaneously
  brings another standby worker up to “ready” state.
* Crash recovery - If a worker dies mid-file, the controller replaces it by
  cloning the last warm worker specification as required by
  ``should_clone_worker``. The new worker goes through the same pre-spawn path
  and resumes the pipeline without breaking isolation.

The scheduler inherits ``LoadFileScheduling`` to reuse collection validation and
work queue construction.  The override focuses on how work is assigned and how
workers are rotated in and out of the pre-spawn pool.
"""

from collections import OrderedDict, deque
from collections.abc import Sequence
from typing import Deque

import pytest

from xdist.remote import Producer
from xdist.scheduler.loadfile import LoadFileScheduling
from xdist.workermanage import WorkerController


class IsolatedScheduler(LoadFileScheduling):
    """Scheduler that enforces per-file isolation with pre-spawned workers.

    The controller keeps one active worker executing tests while the next worker
    is already started and ready to take over. Each worker receives the tests
    from a single file and is shut down immediately after completing them,
    ensuring process-level isolation between files while still overlapping
    worker start-up with current execution.
    """

    def __init__(self, config: pytest.Config, log: Producer | None = None) -> None:
        super().__init__(config, log)
        # Only require the first two workers (or fewer if less is configured) to
        # finish collection before scheduling. This lets the active worker start
        # promptly while ensuring we already have a warm standby.
        self.numnodes = max(1, min(2, self.numnodes))
        self.active_worker: WorkerController | None = None
        self.next_worker: WorkerController | None = None
        self._standby_workers: Deque[WorkerController] = deque()
        self._current_scope: dict[WorkerController, str] = {}
        self._nodes_to_clone_after_finish: set[WorkerController] = set()
        self._active_waiting_for_collection: WorkerController | None = None

    # ------------------------------------------------------------------
    # Public API used by DSession
    # ------------------------------------------------------------------

    def add_node(self, node: WorkerController) -> None:
        super().add_node(node)
        # First ready worker becomes the active executor.
        if self.active_worker is None:
            self.active_worker = node
            self._active_waiting_for_collection = None
            self._maybe_start_active()
            return

        if self.next_worker is None:
            self.next_worker = node
        else:
            self._standby_workers.append(node)

        # Try to keep the pipeline full: we want both active and next slots populated.
        self._fill_next_worker()
        self._update_clone_request_for_active()

    def remove_node(self, node: WorkerController) -> str | None:  # type: ignore[override]
        should_clone = node in self._nodes_to_clone_after_finish
        self._current_scope.pop(node, None)
        if node is self._active_waiting_for_collection:
            self._active_waiting_for_collection = None

        was_active = node is self.active_worker
        was_next = node is self.next_worker

        if was_active:
            self.active_worker = None
        elif was_next:
            self.next_worker = None
        else:
            try:
                self._standby_workers.remove(node)
            except ValueError:
                pass

        if node not in self.assigned_work:
            return None

        crashitem = super().remove_node(node)

        if was_active:
            self._promote_next_worker_to_active_if_missing()
            self._maybe_start_active()
        elif was_next:
            self._fill_next_worker()
            self._update_clone_request_for_active()

        if should_clone:
            self._nodes_to_clone_after_finish.add(node)

        return crashitem

    def add_node_collection(
        self, node: WorkerController, collection: Sequence[str]
    ) -> None:
        super().add_node_collection(node, collection)

        if node is self._active_waiting_for_collection:
            self._active_waiting_for_collection = None
            self._maybe_start_active()
        elif node is self.next_worker:
            self._update_clone_request_for_active()

    def schedule(self) -> None:
        assert self.collection_is_completed

        if self.collection is not None:
            # Already initialised once – just check if we should dispatch more work.
            self._maybe_start_active()
            return

        if not self._check_nodes_have_same_collection():
            self.log("**Different tests collected, aborting run**")
            return

        self.collection = list(next(iter(self.registered_collections.values())))
        if not self.collection:
            self._shutdown_unneeded_workers()
            return

        unsorted_workqueue: OrderedDict[str, dict[str, bool]] = OrderedDict()
        for nodeid in self.collection:
            scope = self._split_scope(nodeid)
            work_unit = unsorted_workqueue.setdefault(scope, OrderedDict())
            work_unit[nodeid] = False

        self.workqueue.update(unsorted_workqueue)

        self._promote_next_worker_to_active_if_missing()
        self._maybe_start_active()

    def mark_test_complete(  # type: ignore[override]
        self, node: WorkerController, item_index: int, duration: float = 0
    ) -> None:
        nodeid = self.registered_collections[node][item_index]
        scope = self._split_scope(nodeid)

        self.assigned_work[node][scope][nodeid] = True

        if all(self.assigned_work[node][scope].values()):
            del self.assigned_work[node][scope]
            self._current_scope.pop(node, None)

            if node is self.active_worker:
                self._finish_active_worker(node)
            else:
                self._request_shutdown(node)

    def should_clone_worker(self, node: WorkerController) -> bool:
        if node not in self._nodes_to_clone_after_finish:
            return False
        self._nodes_to_clone_after_finish.remove(node)

        future_scopes = len(self.workqueue)
        if future_scopes:
            return True

        future_workers = (1 if self.next_worker is not None else 0) + len(
            self._standby_workers
        )
        return bool(future_workers)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finish_active_worker(self, node: WorkerController) -> None:
        self._request_shutdown(node)
        self.active_worker = None
        self._promote_next_worker_to_active_if_missing()
        self._maybe_start_active()

    def _promote_next_worker_to_active_if_missing(self) -> None:
        if self.active_worker is not None:
            return
        if self.next_worker is not None:
            self.active_worker = self.next_worker
            self.next_worker = None
            self._active_waiting_for_collection = None
            return
        self._fill_next_worker()
        if self.next_worker is not None:
            self.active_worker = self.next_worker
            self.next_worker = None
            self._active_waiting_for_collection = None

    def _fill_next_worker(self) -> None:
        if self.next_worker is not None:
            return
        while self._standby_workers:
            candidate = self._standby_workers.popleft()
            if candidate is self.active_worker:
                continue
            if candidate in self.assigned_work:
                self.next_worker = candidate
                break

    def _maybe_start_active(self) -> None:
        if self.collection is None:
            return

        self._promote_next_worker_to_active_if_missing()

        node = self.active_worker
        if node is None:
            return

        if node not in self.registered_collections:
            # Node is ready but hasn't delivered its collection yet.
            self._active_waiting_for_collection = node
            return

        if node in self._current_scope:
            return

        if not self.workqueue:
            self._shutdown_unneeded_workers()
            return

        scope, work_unit = self.workqueue.popitem(last=False)
        # Assign the next file-sized chunk and record which worker owns it.
        self.assigned_work[node][scope] = work_unit
        self._current_scope[node] = scope

        worker_collection = self.registered_collections[node]
        nodeids_indexes = [
            worker_collection.index(nodeid)
            for nodeid, completed in work_unit.items()
            if not completed
        ]
        node.send_runtest_some(nodeids_indexes)
        # Isolation guarantee: mark this worker for shutdown once it finishes.
        self._request_shutdown(node)

        self._fill_next_worker()

        if not self.workqueue:
            self._shutdown_unneeded_workers()

        self._update_clone_request_for_active()

    def _shutdown_unneeded_workers(self) -> None:
        if self.workqueue:
            return

        if self.next_worker is not None:
            worker = self.next_worker
            self.next_worker = None
            self._request_shutdown(worker)

        while self._standby_workers:
            worker = self._standby_workers.popleft()
            self._request_shutdown(worker)

        if self.active_worker is not None:
            self._request_shutdown(self.active_worker)

        self._update_clone_request_for_active()

    def _update_clone_request_for_active(self) -> None:
        node = self.active_worker
        if node is None or node not in self._current_scope:
            return

        future_scopes = len(self.workqueue)
        future_worker_capacity = (1 if self.next_worker is not None else 0) + len(
            self._standby_workers
        )

        if future_scopes > future_worker_capacity:
            self._nodes_to_clone_after_finish.add(node)
        else:
            self._nodes_to_clone_after_finish.discard(node)

    def _request_shutdown(self, node: WorkerController) -> None:
        if not getattr(node, "_shutdown_sent", False):
            node.shutdown()
