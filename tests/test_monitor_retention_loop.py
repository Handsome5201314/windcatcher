"""The idle retention task stops with the service and reports retryable errors."""

import asyncio
from types import SimpleNamespace

import server


class DummyQueue:
    def __init__(self, *_args, **_kwargs):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def health(self):
        return {"worker_alive": self.started and not self.stopped}


def test_personal_lifespan_retries_cleanup_and_stops_task(monkeypatch, caplog):
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    monkeypatch.setattr(server, "_MONITOR_RETENTION_CHECK_SECONDS", 0.01)
    queues = []

    class TrackedQueue(DummyQueue):
        def __init__(self, *_args, **kwargs):
            super().__init__(*_args, **kwargs)
            queues.append(self)

    monkeypatch.setattr(server.batch_jobs, "BatchQueue", TrackedQueue)
    monkeypatch.setattr(server, "_record_activity", lambda _event: 1)
    calls = []

    def cleanup():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("retryable test failure")

    monkeypatch.setattr(server.activity_monitor, "prune_expired_events", cleanup)
    application = SimpleNamespace(state=SimpleNamespace(interrupted_cases_recovered=True))

    async def run():
        async with server.app_lifespan(application):
            async def twice():
                while len(calls) < 2:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(twice(), timeout=2)
            assert len(queues) == 2 and all(queue.started for queue in queues)
        assert all(queue.stopped for queue in queues)
        previous = len(calls)
        await asyncio.sleep(0.05)
        assert len(calls) == previous

    asyncio.run(run())
    assert "Activity retention cleanup failed: OSError" in caplog.text


def test_shared_lifespan_does_not_schedule_cleanup(monkeypatch):
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    queues = []

    class TrackedQueue(DummyQueue):
        def __init__(self, *_args, **kwargs):
            super().__init__(*_args, **kwargs)
            queues.append(self)

    monkeypatch.setattr(server.batch_jobs, "BatchQueue", TrackedQueue)
    monkeypatch.setattr(server, "_record_activity", lambda _event: 1)
    calls = []
    monkeypatch.setattr(server.activity_monitor, "prune_expired_events", lambda: calls.append(1))
    application = SimpleNamespace(state=SimpleNamespace(interrupted_cases_recovered=True))

    async def run():
        async with server.app_lifespan(application):
            await asyncio.sleep(0.03)

    asyncio.run(run())
    assert len(queues) == 2 and all(queue.started and queue.stopped for queue in queues)
    assert calls == []
