"""Portable runner contracts; every host/process identity is synthetic."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

import probe_run as runner
from probe_target import PreflightError, VerifiedTarget


def metadata(pid=101, receive=False):
    return {"type": "send", "payload": {"kind": "metadata_verified", "pid": pid, "receive": receive}}


def cleanup(pid=101):
    return {"host_lifecycle": "complete", "cleanup_verified": True, "mode": "probe", "pid": pid,
            "metadata_ready": True, "ok": True}


class FakeHost:
    def __init__(self, terminate_runner=False):
        self.returncode = None if terminate_runner else 0
        self.terminate_runner = terminate_runner
        self.terminated = self.killed = 0

    def poll(self):
        if self.terminate_runner:
            self.terminate_runner = False
            signal.raise_signal(signal.SIGTERM)
        return self.returncode

    def terminate(self):
        self.terminated += 1
        # A second request during cleanup must not skip waiting for our host.
        signal.raise_signal(signal.SIGTERM)
        self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed += 1
        self.returncode = -signal.SIGKILL

    def wait(self, timeout):
        return self.returncode


class ProbeRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / ".probe-local/bin").mkdir(parents=True)
        host = self.root / ".probe-local/bin/probe-frida-host"
        host.write_text("synthetic placeholder; never executed\n")
        host.chmod(0o700)
        (self.root / "tools").mkdir()
        (self.root / "tools/probe_observer.js").write_text("// Synthetic source; never executed.\n")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(runner, "ROOT", self.root).start()
        self.profile = str(self.root / "synthetic-profile.json")
        self.target = VerifiedTarget(
            pid=101, uid=os.getuid(), start_seconds=10, start_microseconds=20,
            app_path="/synthetic/WeChat-Main-Probe.app", executable_path="/synthetic/WeChat",
            module_path="/synthetic/wechat.dylib", arm64_uuid="synthetic", arm64_sha256="synthetic",
            supervisor_pid=102, debugger_pid=103,
            supervisor_identity={"pid": 102, "uid": os.getuid(), "start_seconds": 10,
                                 "start_microseconds": 21, "executable_path": "/synthetic/supervisor"},
            debugger_identity={"pid": 103, "uid": os.getuid(), "start_seconds": 10,
                               "start_microseconds": 22, "executable_path": "/synthetic/debugger"})
        self.validator = mock.Mock(return_value=self.target)

    def fake_spawn(self, events, host=None):
        host = host or FakeHost()
        def spawn(argv, **kwargs):
            self.assertEqual(argv[1:3], ["--probe-profile", self.profile])
            self.assertEqual(Path(argv[0]), self.root / ".probe-local/bin/probe-frida-host")
            for event in events:
                kwargs["stdout"].write(json.dumps(event) + "\n")
            kwargs["stdout"].flush()
            return host
        return mock.Mock(side_effect=spawn)

    def execute(self, spawn, mode="metadata", validator=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.run(self.profile, mode, 1, validator=validator or self.validator, spawn=spawn)

    def result(self):
        paths = list((self.root / ".probe-local").glob("run-*/result.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_invalid_profile_performs_zero_spawns(self):
        spawn = mock.Mock()
        validator = mock.Mock(side_effect=PreflightError("invalid_profile"))
        with self.assertRaisesRegex(PreflightError, "invalid_profile"):
            self.execute(spawn, validator=validator)
        spawn.assert_not_called()
        self.assertEqual(list((self.root / ".probe-local").glob("run-*")), [])

    def test_owner_generation_changed_before_launch_performs_zero_spawns(self):
        changed = replace(self.target, debugger_identity={**self.target.debugger_identity,
                                                         "start_microseconds": 23})
        spawn = mock.Mock()
        validator = mock.Mock(side_effect=[self.target, changed])
        with self.assertRaisesRegex(PreflightError, "target_generation_changed"):
            self.execute(spawn, validator=validator)
        spawn.assert_not_called()

    def test_metadata_contract_requires_cleanup_marker(self):
        self.assertEqual(self.execute(self.fake_spawn([metadata()])), 1)
        result = self.result()
        self.assertTrue(result["events"]["ready"])
        self.assertFalse(result["events"]["cleanup_verified"])
        self.assertFalse(result["pass"])

    def test_lifecycle_cannot_hide_host_failure(self):
        path = self.root / "events.log"
        for field in ("cleanup_verified", "metadata_ready", "ok"):
            for value in (False, None, "true"):
                with self.subTest(field=field, value=value):
                    event = {**cleanup(), field: value}
                    path.write_text("\n".join(json.dumps(e) for e in (metadata(), event)))
                    self.assertFalse(runner.inspect_log(path, "metadata", 101)["cleanup_verified"])

    def test_metadata_mode_rejects_observer_and_sample_events(self):
        path = self.root / "events.log"
        for extra in ({"type": "send", "payload": {"kind": "observer_ready"}},
                      {"type": "send", "payload": {"kind": "sample"}},
                      metadata(receive=True), metadata(pid=999)):
            with self.subTest(extra=extra):
                path.write_text("\n".join(json.dumps(e) for e in (metadata(), extra, cleanup())))
                parsed = runner.inspect_log(path, "metadata", 101)
                self.assertFalse(parsed["ready"])
                self.assertTrue(parsed["mode_error"])

    def test_observe_mode_requires_observer_ready(self):
        self.assertEqual(self.execute(self.fake_spawn([metadata(receive=True), cleanup()]), "observe"), 1)
        self.assertFalse(self.result()["events"]["ready"])

    def test_metadata_contract_has_fixed_source_and_test_scope(self):
        self.assertEqual(self.execute(self.fake_spawn([metadata(), cleanup()])), 0)
        result = self.result()
        self.assertTrue(result["pass"])
        self.assertFalse(result["sends_enabled"])
        self.assertRegex(result["prefix"], r"^WECHAT_AI_PROBE_[a-f0-9]{16}:$")
        source = next((self.root / ".probe-local").glob("run-*/observer.js")).read_text()
        settings = json.loads(source.split("const SETTINGS = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(settings["conversation"], "filehelper")
        self.assertFalse(settings["observe"])
        self.assertEqual(settings["prefix"], result["prefix"])

    def test_sigterm_cleans_only_own_host_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        host = FakeHost(terminate_runner=True)
        spawn = self.fake_spawn([metadata(), cleanup()], host)
        with mock.patch("os.kill", side_effect=AssertionError("target/owner signals forbidden")):
            self.assertEqual(self.execute(spawn), 1)
        self.assertEqual(host.terminated, 1)
        self.assertEqual(host.killed, 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        result = self.result()
        self.assertEqual(result["error"], "runner_terminated")
        self.assertFalse(result["pass"])


if __name__ == "__main__":
    unittest.main()
