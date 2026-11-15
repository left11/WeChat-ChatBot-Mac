"""Identity refusal tests use synthetic files and a fake read-only kernel."""

import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest import mock
import uuid

import probe_target as probe


class FakeKernel:
    def __init__(self, identities):
        self.identities = identities
        self.reads = []
        self.after_read = None

    def identity(self, pid):
        self.reads.append(pid)
        value = copy.deepcopy(self.identities[pid])
        if self.after_read:
            self.after_read(len(self.reads), self.identities)
        return value


class ProbeTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / "WeChat-Main-Probe.app"
        (self.app / "Contents/MacOS").mkdir(parents=True)
        (self.app / "Contents/Resources").mkdir()
        self.exe = self.app / "Contents/MacOS/WeChat"
        self.exe.write_bytes(b"synthetic executable; never run")
        self.info_path = self.app / "Contents/Info.plist"
        self.info = {"CFBundleIdentifier": probe.BUNDLE_ID, "CFBundleExecutable": "WeChat",
                     "WeChatBundleVersion": probe.FULL_VERSION,
                     "CFBundleShortVersionString": "4.1.15", "CFBundleVersion": probe.BUILD}
        self.info_path.write_bytes(plistlib.dumps(self.info))
        header = struct.pack("<8I", 0xFEEDFACF, 0x100000C, 0, 6, 1, 24, 0, 0)
        self.slice = header + struct.pack("<II", 0x1B, 24) + uuid.UUID(probe.ARM64_UUID).bytes
        fat = b"\xca\xfe\xba\xbe" + struct.pack(">I", 1)
        fat += struct.pack(">IIIII", 0x100000C, 0, 32, len(self.slice), 0) + b"\0" * 4
        self.module = self.app / "Contents/Resources/wechat.dylib"
        self.module.write_bytes(fat + self.slice)
        digest = hashlib.sha256(self.slice).hexdigest()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(probe, "ARM64_SHA256", digest).start()
        self.supervisor_exe = self.root / "supervisor-executable"
        self.supervisor_exe.write_bytes(b"not executable")
        self.debugger_exe = self.root / "debugger-executable"
        self.debugger_exe.write_bytes(b"not executable")
        self.trace = self.root / "trace"
        self.trace.mkdir()
        self.state_path = self.root / "resident-controller.json"
        self.result_path = self.trace / "debugger-result.json"
        self.state = {"resident": True, "phase": "debugger_started", "trace_dir": str(self.trace),
                      "supervisor_pid": 102, "debugger_pid": 103}
        self.result = {"resident": True, "observation_state": "active", "pid": 101,
                       "heartbeat_at_unix": 999.0}
        self.profile_path = self.root / "profile.json"
        self.profile = {
            "schema_version": 1,
            "target": {"app_path": str(self.app), "executable_path": str(self.exe),
                       "module_path": str(self.module), "bundle_id": probe.BUNDLE_ID,
                       "full_version": probe.FULL_VERSION, "build": probe.BUILD,
                       "arm64_uuid": probe.ARM64_UUID, "arm64_sha256": digest,
                       "process": self.identity(101, self.exe)},
            "controller": {"state_path": str(self.state_path), "result_path": str(self.result_path),
                           "trace_dir": str(self.trace), "max_heartbeat_age_seconds": 8,
                           "supervisor": self.identity(102, self.supervisor_exe),
                           "debugger": self.identity(103, self.debugger_exe)}}
        values = [self.profile["target"]["process"], self.profile["controller"]["supervisor"],
                  self.profile["controller"]["debugger"]]
        self.kernel = FakeKernel({value["pid"]: {**value, "flags": 2, "status": 3,
                                                "ppid": 102 if value["pid"] == 103 else 1}
                                  for value in values})
        self.operation = mock.Mock(return_value="called")
        self.save()

    @staticmethod
    def identity(pid, executable):
        return {"pid": pid, "uid": os.getuid(), "start_seconds": 100,
                "start_microseconds": 456, "executable_path": str(executable)}

    def save(self):
        for path, value in ((self.profile_path, self.profile), (self.state_path, self.state),
                            (self.result_path, self.result)):
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def run_operation(self):
        return probe.with_verified_target(str(self.profile_path), self.operation,
                                          kernel=self.kernel, now=1000.0)

    def assert_refused(self, code=None):
        with self.assertRaises(probe.PreflightError) as raised:
            self.run_operation()
        if code:
            self.assertEqual(str(raised.exception), code)
        self.operation.assert_not_called()

    def test_explicit_pinned_profile_permits_one_callback(self):
        self.assertEqual(self.run_operation(), "called")
        self.operation.assert_called_once()
        target = self.operation.call_args.args[0]
        self.assertEqual(target.pid, 101)
        self.assertTrue(target.controller_verified)
        self.assertEqual(target.arm64_sha256, hashlib.sha256(self.slice).hexdigest())
        self.assertEqual(target.supervisor_identity, self.profile["controller"]["supervisor"])
        self.assertEqual(target.debugger_identity, self.profile["controller"]["debugger"])
        self.assertEqual(target.as_dict()["supervisor_identity"], target.supervisor_identity)
        self.assertEqual(self.kernel.reads, [101, 102, 103, 101, 102, 103])

    def test_owner_rebirth_with_updated_profile_changes_verified_generation(self):
        original = probe.preflight(str(self.profile_path), kernel=self.kernel, now=1000.0)
        for kind, pid in (("supervisor", 102), ("debugger", 103)):
            with self.subTest(kind=kind):
                self.profile["controller"][kind]["start_microseconds"] += 1
                self.kernel.identities[pid]["start_microseconds"] += 1
                self.save()
                changed = probe.preflight(str(self.profile_path), kernel=self.kernel, now=1000.0)
                self.assertEqual(changed.pid, original.pid)
                self.assertEqual(changed.supervisor_pid, original.supervisor_pid)
                self.assertEqual(changed.debugger_pid, original.debugger_pid)
                self.assertNotEqual(changed, original)
                self.profile["controller"][kind]["start_microseconds"] -= 1
                self.kernel.identities[pid]["start_microseconds"] -= 1

    def test_no_profile_has_no_discovery_or_action(self):
        with self.assertRaisesRegex(probe.PreflightError, "explicit_profile_required"):
            probe.with_verified_target(None, self.operation, kernel=self.kernel)
        self.operation.assert_not_called()
        self.assertEqual(self.kernel.reads, [])

    def test_malformed_profile_is_refused_without_action(self):
        for field in ("target", "controller"):
            with self.subTest(field=field):
                original = self.profile[field]
                self.profile[field] = []
                self.save()
                self.assert_refused("explicit_" + field + "_required")
                self.profile[field] = original

    def test_daily_bundle_is_rejected_before_kernel_access(self):
        self.info["CFBundleIdentifier"] = "com.tencent.xinWeChat"
        self.info_path.write_bytes(plistlib.dumps(self.info))
        self.assert_refused("app_identity_mismatch")
        self.assertEqual(self.kernel.reads, [])

    def test_profile_cannot_select_other_bundle_or_version(self):
        for key, value in (("bundle_id", "com.tencent.xinWeChat"),
                           ("full_version", "4.1.15.55"), ("build", "270135"),
                           ("arm64_uuid", "wrong"), ("arm64_sha256", "wrong")):
            with self.subTest(key=key):
                original = self.profile["target"][key]
                self.profile["target"][key] = value
                self.save()
                self.assert_refused("unsupported_target_profile")
                self.profile["target"][key] = original

    def test_process_birth_microseconds_and_uid_are_pinned(self):
        for key, value in (("start_seconds", 101), ("start_microseconds", 457),
                           ("uid", os.getuid() + 1), ("pid", 999),
                           ("executable_path", str(self.supervisor_exe))):
            with self.subTest(key=key):
                original = self.kernel.identities[101][key]
                self.kernel.identities[101][key] = value
                self.assert_refused("process_identity_changed")
                self.kernel.identities[101][key] = original

    def test_reused_pid_during_hashing_is_rejected(self):
        def change_after_first_round(count, identities):
            if count == 3:
                identities[101]["start_microseconds"] += 1
        self.kernel.after_read = change_after_first_round
        self.assert_refused("process_identity_changed")

    def test_module_leaf_symlink_rejected(self):
        original = self.root / "original-module"
        self.module.rename(original)
        self.module.symlink_to(original)
        self.assert_refused("symlink_or_noncanonical_path")
        self.assertEqual(self.kernel.reads, [])

    def test_symlink_parent_rejected(self):
        link = self.root / "app-alias"
        link.symlink_to(self.app, target_is_directory=True)
        self.profile["target"]["app_path"] = str(link)
        self.save()
        self.assert_refused("symlink_or_noncanonical_path")

    def test_module_hash_change_rejected(self):
        data = bytearray(self.module.read_bytes())
        data[40] = 1  # CPU subtype remains syntactically valid, hash differs.
        self.module.write_bytes(data)
        self.assert_refused("module_hash_mismatch")

    def test_module_uuid_change_rejected(self):
        data = bytearray(self.module.read_bytes())
        data[-1] ^= 1
        self.module.write_bytes(data)
        self.assert_refused("module_uuid_mismatch")

    def test_stale_future_and_nan_heartbeat_rejected(self):
        for heartbeat in (991.9, 1000.1, float("nan")):
            with self.subTest(heartbeat=heartbeat):
                self.result["heartbeat_at_unix"] = heartbeat
                self.save()
                self.assert_refused("controller_heartbeat_stale")

    def test_wrong_debugger_parent_rejected(self):
        self.kernel.identities[103]["ppid"] = 888
        self.assert_refused("controller_parent_mismatch")

    def test_wrong_controller_process_or_target_rejected(self):
        for kind in ("debugger", "supervisor"):
            with self.subTest(kind=kind):
                identity = self.kernel.identities[103 if kind == "debugger" else 102]
                identity["start_microseconds"] += 1
                self.assert_refused("process_identity_changed")
                identity["start_microseconds"] -= 1
        self.result["pid"] = 999
        self.save()
        self.assert_refused("controller_target_mismatch")

    def test_owner_stopping_rejected(self):
        self.state["phase"] = "stop_requested"
        self.save()
        self.assert_refused("controller_state_mismatch")

    def test_traced_stopped_target_with_verified_owner_is_accepted(self):
        self.kernel.identities[101]["flags"] = 2  # PROC_FLAG_TRACED, proc_info.h
        self.kernel.identities[101]["status"] = 4
        self.assertEqual(self.run_operation(), "called")

    def test_traced_target_with_stale_owner_is_refused(self):
        self.kernel.identities[101]["flags"] = 2  # Tracing never bypasses owner validation.
        self.result["heartbeat_at_unix"] = 990
        self.save()
        self.assert_refused("controller_heartbeat_stale")

    def test_zombie_target_is_refused_regardless_of_flags(self):
        self.kernel.identities[101]["status"] = 5  # SZOMB, proc.h
        for flags in (0, 2):
            with self.subTest(flags=flags):
                self.kernel.identities[101]["flags"] = flags
                self.assert_refused("process_not_live")

    def test_missing_file_and_relative_path_fail_closed(self):
        self.profile_path.unlink()
        self.assert_refused("invalid_or_unavailable_profile_data")
        with self.assertRaisesRegex(probe.PreflightError, "noncanonical_path"):
            probe.with_verified_target("profile.json", self.operation, kernel=self.kernel)
        self.operation.assert_not_called()

    def test_zero_process_mutations_on_failure(self):
        # Trap accidental additions of common mutation/discovery mechanisms.
        self.result["heartbeat_at_unix"] = 1
        self.save()
        with (mock.patch("os.kill", side_effect=AssertionError("signal forbidden")) as kill,
              mock.patch("subprocess.Popen", side_effect=AssertionError("spawn forbidden")) as popen,
              mock.patch("socket.socket", side_effect=AssertionError("network forbidden")) as socket):
            self.assert_refused("controller_heartbeat_stale")
            kill.assert_not_called()
            popen.assert_not_called()
            socket.assert_not_called()


if __name__ == "__main__":
    unittest.main()
