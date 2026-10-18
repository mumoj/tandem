from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tandem.config import TaskPaths
from tandem.notify import Notifier
from tandem.supervisor.activity import ActivityTracker
from tandem.supervisor.journal import Journal
from tandem.supervisor.protected import ProtectedFiles
from tandem.supervisor.state import StateStore
from tandem.supervisor.supervisor import Supervisor
from tests.fakes import FakeClock, FakeOpenCode

SupervisorFactory = Callable[..., Supervisor]


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "task"
    directory.mkdir()
    (directory / "instruction.md").write_text("Make the tests pass.\n")
    return directory


@pytest.fixture
def paths(tmp_path: Path, task_dir: Path) -> TaskPaths:
    task_paths = TaskPaths.for_task(tmp_path / "state", task_dir)
    task_paths.ensure()
    return task_paths


@pytest.fixture
def api() -> FakeOpenCode:
    return FakeOpenCode()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_supervisor(
    api: FakeOpenCode, paths: TaskPaths, task_dir: Path, clock: FakeClock
) -> SupervisorFactory:
    """Build a supervisor over the fake; call again to simulate a restart."""

    def build(protected: tuple[str, ...] = ("instruction.md",)) -> Supervisor:
        return Supervisor(
            api=api,
            paths=paths,
            task_dir=task_dir,
            store=StateStore(paths.state, task_dir),
            journal=Journal(paths.journal),
            notifier=Notifier(log_path=paths.notifications, task="task", state_dir=paths.root),
            tracker=ActivityTracker(clock=clock),
            protected=ProtectedFiles(task_dir, protected),
            abort_settle=0.05,
        )

    return build


@pytest.fixture
def supervisor(make_supervisor: SupervisorFactory) -> Supervisor:
    return make_supervisor()
