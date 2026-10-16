import os
import pytest
from harnessfleet import FleetError


def test_fatal_progress_flush_cannot_publish_verified_output(instance, monkeypatch):
    fault = instance.path / "fault"
    fault.mkdir()
    (fault / "sitecustomize.py").write_text("""
import os,sys
if 'harnessfleet.worker' in sys.orig_argv:
    from harnessfleet import progress
    class FatalProgress:
        def __init__(self, *args):
            self.failure_fd, self.write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        def submit(self, *args):pass
        def raise_if_failed(self):pass
        def finish(self):raise SystemExit('synthetic fatal progress flush')
        def cancel(self):pass
        def join(self):
            os.close(self.failure_fd);os.close(self.write_fd)
            return True
    progress.ProgressWriter = FatalProgress
""")
    monkeypatch.setenv("PYTHONPATH", str(fault) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    run = instance.m.start("codex-local", "mode:normal", delegation="leaf")
    final = instance.finish(run)
    assert final["status"] == "failed" and not final["accepted"]
    assert final["cleanup_confirmed"]
    with pytest.raises(FleetError) as error:
        instance.m.output(run["id"])
    assert error.value.code == "no_final_output"
