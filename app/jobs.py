"""Background jobs: profiling, relationship checks and scans of a scripts folder.

A job runs in a thread of this process. It survives closing the browser tab, not a restart of
the app. There is at most ONE running job per workspace, whatever its kind, so a workspace's
database never has two of the app's jobs on it at once.

    jobs.start(...)      start a job; Busy if the workspace already has one running
    jobs.get / cancel    progress, and asking it to stop (it stops before its next step)
    jobs.saving(job)     wrap every write of a job's results: a job that was cancelled, or whose
                         source or workspace has been removed, writes nothing any more
    jobs.stop_for(...)   called before a source or workspace is removed: cancels its running job
                         and seals it, so nothing is saved for it afterwards
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from .security import sanitize_error
from .workspace import NotFound, new_id

KEEP_JOBS = 20


class Busy(Exception):
    """A job is already running in this workspace."""

    def __init__(self, job):
        what = {'profile': 'A profiling job', 'relations': 'A relationship check',
                'scripts': 'A scan of scripts'}.get(job.get('kind'), 'A job')
        super().__init__(f'{what} is already running in this workspace.')
        self.job = job


class Cancelled(Exception):
    """The job was cancelled; raised between two steps."""


class Failed(Exception):
    """The job cannot go on, for a reason meant for the user: its text is shown as it is."""


def _now():
    return datetime.now().isoformat(timespec='seconds')


@dataclass
class Job:
    id: str
    workspace_id: str
    kind: str                           # profile / relations
    total: int
    sources: frozenset = frozenset()    # ids of the sources it reads
    options: dict = field(default_factory=dict)
    state: str = 'running'              # running / done / cancelled / failed
    phase: str = ''
    done: int = 0
    current: str | None = None
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error: str | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    _started: float = field(default_factory=time.monotonic)
    _elapsed: float | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)
    _sealed: bool = False               # its source or workspace is gone: it may not write any more
    _thread: threading.Thread | None = None

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def fail(self, item, message, **extra):
        """Record that one item (a table, a relationship) could not be done."""
        self.errors.append({'table': item, 'message': sanitize_error(message, limit=300), **extra})

    def to_dict(self):
        elapsed = self._elapsed if self._elapsed is not None else time.monotonic() - self._started
        running = self.state == 'running'
        return {'id': self.id, 'workspace_id': self.workspace_id, 'kind': self.kind, 'state': self.state,
                'phase': self.phase, **self.options,
                'done': self.done, 'total': self.total, 'current': self.current if running else None,
                'tables_done': self.done, 'tables_total': self.total,
                'current_table': self.current if running else None,
                'cancel_requested': self.cancelled and running,
                'elapsed_s': round(elapsed, 1), 'started_at': self.started_at, 'finished_at': self.finished_at,
                'errors': list(self.errors), 'notes': list(self.notes), 'error': self.error}


class Jobs:
    def __init__(self):
        self._jobs = {}                 # job id -> Job. Memory only: jobs do not survive a restart.
        self._lock = threading.RLock()

    def start(self, workspace_id, kind, total, run, sources=(), phase='', **options):
        """Start `run(job)` in a thread. Raises Busy when the workspace already has a running job."""
        with self._lock:
            busy = self.running(workspace_id)
            if busy is not None:
                raise Busy(busy.to_dict())
            job = Job(new_id(), workspace_id, kind, int(total), frozenset(sources), dict(options), phase=phase)
            self._jobs[job.id] = job
            for old in list(self._jobs)[:-KEEP_JOBS]:
                if self._jobs[old].state != 'running':
                    del self._jobs[old]
            job._thread = threading.Thread(target=self._run, args=(job, run), name=f'{kind}-{job.id}', daemon=True)
        job._thread.start()
        return job

    @staticmethod
    def _run(job, run):
        try:
            run(job)
            job.state = 'cancelled' if job.cancelled else 'done'
        except Cancelled:
            job.state = 'cancelled'
        except Failed as exc:
            job.state, job.error = 'failed', sanitize_error(str(exc), limit=300)
        except Exception as exc:
            job.state, job.error = 'failed', sanitize_error(f'{type(exc).__name__}: {exc}', limit=300)
        finally:
            job._elapsed = time.monotonic() - job._started
            job.finished_at = _now()
            job.current = None

    def find(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFound('Job not found. Jobs are kept in memory and are gone after the app restarts.')
        return job

    def get(self, job_id):
        return self.find(job_id).to_dict()

    def running(self, workspace_id):
        with self._lock:
            return next((j for j in self._jobs.values() if j.workspace_id == workspace_id and j.state == 'running'), None)

    def latest(self, workspace_id, kind=None):
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.workspace_id == workspace_id and kind in (None, j.kind)]
        return jobs[-1] if jobs else None

    def cancel(self, job_id):
        job = self.find(job_id)
        job._cancel.set()
        return job.to_dict()

    def wait(self, job_id, timeout=120):
        """Block until a job has finished (for tests and scripts)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.get(job_id)
            if state['state'] != 'running':
                return state
            time.sleep(0.02)
        raise TimeoutError(f'job {job_id} still running after {timeout} s')

    @contextmanager
    def saving(self, job=None):
        """Hold this around a write of results. Raises Cancelled instead of entering when the job
        was cancelled or sealed, so a write can never land after its source was removed."""
        with self._lock:
            if job is not None and (job._sealed or job.cancelled):
                raise Cancelled()
            yield

    def stop_for(self, workspace_id, source_id=None, wait_s=15):
        """Before a source (or, with source_id None, a whole workspace) is removed: cancel the
        running job that reads it and seal it. When this returns, that job writes nothing more.
        Waits a little for its thread to end, so files it had open can be deleted."""
        with self._lock:                # no save is in flight while the lock is held
            hit = [j for j in self._jobs.values() if j.workspace_id == workspace_id and j.state == 'running'
                   and (source_id is None or source_id in j.sources)]
            for job in hit:
                job._cancel.set()
                job._sealed = True
        deadline = time.monotonic() + wait_s
        for job in hit:
            if job._thread is not None and job._thread is not threading.current_thread():
                job._thread.join(max(0.0, deadline - time.monotonic()))
        return [job.id for job in hit]
