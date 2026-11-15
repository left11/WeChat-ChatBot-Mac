#!/usr/bin/env python3
"""Read-only, explicit-profile identity checks for the isolated macOS Probe.

No process discovery, attach, launch, signal, socket, or account-data access.
The caller owns any later operation; validation is not a replacement for its
own race/lifecycle checks and does not grant permission to replace the owner.
"""

import argparse
import ctypes
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import plistlib
import stat
import struct
import sys
import time
import uuid

BUNDLE_ID = "local.wechat.urlprobe.client"
FULL_VERSION = "4.1.15.35"
BUILD = "270115"
ARM64_UUID = "9CEB6CCF-81A5-3B7F-8FB5-B4256FE67EEB"
ARM64_SHA256 = "6d3feb56043cb63d2ff3f77aa32115abbfe7a2f28b3ab1ab59886ee625703e4b"


class PreflightError(ValueError):
    """A stable error code; no profile contents or account data in messages."""


def require(condition, code):
    if not condition:
        raise PreflightError(code)


def canonical_path(value, *, directory=False):
    require(isinstance(value, str) and value and "\0" not in value, "invalid_path")
    path = Path(value)
    require(path.is_absolute() and str(path) == value and ".." not in path.parts,
            "noncanonical_path")
    require(str(path.resolve(strict=True)) == value, "symlink_or_noncanonical_path")
    current = path
    while current != current.parent:
        require(not current.is_symlink(), "symlink_path")
        current = current.parent
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            "wrong_path_type")
    return path


def read_small(path, limit=1048576):
    # O_NOFOLLOW closes the leaf-symlink race; canonical checks run again after
    # preflight. No result from a path that changed identity is returned.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "invalid_metadata_file")
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022,
                "untrusted_metadata_permissions")
        chunks = []
        size = 0
        while chunk := os.read(fd, min(65536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            require(size <= limit, "metadata_too_large")
        require(os.stat(path, follow_symlinks=False) == os.fstat(fd), "metadata_changed")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_json(path):
    value = json.loads(read_small(path))
    require(isinstance(value, dict), "invalid_json_object")
    return value


class BSDInfo(ctypes.Structure):
    # Apple sys/proc_info.h, PROC_PIDTBSDINFO=3 (no process mutation).
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "exit_status", "pid", "ppid", "uid", "gid",
        "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("names", ctypes.c_char * 48)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "nfiles", "pgid", "job_count", "tty_device", "tty_pgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_seconds", ctypes.c_uint64),
                ("start_microseconds", ctypes.c_uint64)]


class MacKernel:
    def __init__(self):
        require(sys.platform == "darwin", "macos_required")
        require(ctypes.sizeof(BSDInfo) == 136 and BSDInfo.start_seconds.offset == 120,
                "unsupported_kernel_layout")
        self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.library.proc_pidinfo.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
        self.library.proc_pidinfo.restype = ctypes.c_int
        self.library.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        self.library.proc_pidpath.restype = ctypes.c_int

    def identity(self, pid):
        info = BSDInfo()
        require(self.library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
                == ctypes.sizeof(info), "kernel_identity_unavailable")
        path = ctypes.create_string_buffer(4096)
        require(self.library.proc_pidpath(pid, path, len(path)) > 0, "kernel_path_unavailable")
        return {"pid": info.pid, "ppid": info.ppid, "uid": info.uid,
                "start_seconds": info.start_seconds, "start_microseconds": info.start_microseconds,
                "executable_path": path.value.decode("utf-8", "strict"),
                "flags": info.flags, "status": info.status}


def expected_process(value):
    require(isinstance(value, dict), "explicit_process_required")
    for name in ("pid", "uid", "start_seconds", "start_microseconds"):
        require(type(value.get(name)) is int, "explicit_process_identity_required")
    require(0 < value["pid"] < 2**31 and value["uid"] == os.getuid()
            and value["start_seconds"] > 0 and 0 <= value["start_microseconds"] < 1000000,
            "invalid_process_identity")
    canonical_path(value.get("executable_path"))
    return value


def check_process(expected, kernel):
    actual = kernel.identity(expected["pid"])
    for name in ("pid", "uid", "start_seconds", "start_microseconds", "executable_path"):
        require(actual.get(name) == expected[name], "process_identity_changed")
    # proc_bsdinfo uses PROC_FLAG_* (sys/proc_info.h), not P_* (sys/proc.h).
    # In particular bit 2 is PROC_FLAG_TRACED, not an exit flag. A verified
    # owner may legitimately trace/stop this target; use pbsi_status here.
    require(actual.get("status") in (2, 3, 4), "process_not_live")
    canonical_path(actual["executable_path"])
    return actual


def inspect_module(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        original = os.fstat(fd)
        require(original.st_size >= 32, "invalid_module")
        with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as data:
            require(data[:4] == b"\xca\xfe\xba\xbe", "universal_module_required")
            count = struct.unpack_from(">I", data, 4)[0]
            require(1 <= count <= 16 and 8 + count * 20 <= len(data), "invalid_fat_header")
            arm = []
            for index in range(count):
                cpu, _, offset, size, _ = struct.unpack_from(">IIIII", data, 8 + index * 20)
                if cpu == 0x100000C:
                    arm.append((offset, size))
            require(len(arm) == 1, "unique_arm64_slice_required")
            base, size = arm[0]
            require(base >= 8 + count * 20 and size >= 32 and base + size <= len(data),
                    "invalid_arm64_slice")
            require(struct.unpack_from("<II", data, base) == (0xFEEDFACF, 0x100000C),
                    "invalid_arm64_header")
            commands, command_bytes = struct.unpack_from("<II", data, base + 16)
            require(commands <= 4096 and 32 + command_bytes <= size, "invalid_load_commands")
            position = base + 32
            uuids = []
            for _ in range(commands):
                require(position + 8 <= base + 32 + command_bytes, "invalid_load_command")
                command, length = struct.unpack_from("<II", data, position)
                require(length >= 8 and position + length <= base + 32 + command_bytes,
                        "invalid_load_command")
                if command == 0x1B:
                    require(length == 24, "invalid_uuid_command")
                    uuids.append(str(uuid.UUID(bytes=data[position + 8:position + 24])).upper())
                position += length
            require(uuids == [ARM64_UUID], "module_uuid_mismatch")
            digest = hashlib.sha256()
            for offset in range(base, base + size, 1048576):
                digest.update(data[offset:min(offset + 1048576, base + size)])
            require(digest.hexdigest() == ARM64_SHA256, "module_hash_mismatch")
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
        require(identity(os.fstat(fd)) == identity(original)
                and identity(os.stat(path, follow_symlinks=False)) == identity(original),
                "module_changed")
    finally:
        os.close(fd)


@dataclass(frozen=True)
class VerifiedTarget:
    pid: int
    uid: int
    start_seconds: int
    start_microseconds: int
    app_path: str
    executable_path: str
    module_path: str
    arm64_uuid: str
    arm64_sha256: str
    supervisor_pid: int
    debugger_pid: int
    supervisor_identity: dict
    debugger_identity: dict
    controller_verified: bool = True

    def as_dict(self):
        return asdict(self)


def preflight(profile_path, *, kernel=None, now=None):
    """Validate an explicitly provided private profile; return its pinned target.

    `kernel` is a read-only identity provider, injectable for deterministic tests.
    No defaults infer a path/PID/version from a running application.
    """
    try:
        return _preflight(profile_path, kernel=kernel, now=now)
    except PreflightError:
        raise
    except (OSError, ValueError, KeyError, TypeError, struct.error, OverflowError):
        raise PreflightError("invalid_or_unavailable_profile_data") from None


def _preflight(profile_path, *, kernel, now):
    require(profile_path is not None, "explicit_profile_required")
    profile_file = canonical_path(str(profile_path))
    profile = read_json(profile_file)
    require(type(profile.get("schema_version")) is int and profile["schema_version"] == 1,
            "unsupported_profile_schema")
    target = profile["target"]
    require(isinstance(target, dict), "explicit_target_required")
    require(target.get("bundle_id") == BUNDLE_ID and target.get("full_version") == FULL_VERSION
            and target.get("build") == BUILD and target.get("arm64_uuid") == ARM64_UUID
            and target.get("arm64_sha256") == ARM64_SHA256, "unsupported_target_profile")
    app = canonical_path(target["app_path"], directory=True)
    require(app.name == "WeChat-Main-Probe.app", "probe_app_required")
    executable = canonical_path(target["executable_path"])
    module = canonical_path(target["module_path"])
    require(executable == app / "Contents/MacOS/WeChat"
            and module == app / "Contents/Resources/wechat.dylib", "probe_paths_mismatch")
    info_path = canonical_path(str(app / "Contents/Info.plist"))
    info = plistlib.loads(read_small(info_path))
    require(info.get("CFBundleIdentifier") == BUNDLE_ID
            and info.get("CFBundleExecutable") == "WeChat"
            and info.get("WeChatBundleVersion") == FULL_VERSION
            and info.get("CFBundleShortVersionString") == "4.1.15"
            and info.get("CFBundleVersion") == BUILD, "app_identity_mismatch")
    expected = expected_process(target["process"])
    require(expected["executable_path"] == str(executable), "process_executable_mismatch")
    owner = profile["controller"]
    require(isinstance(owner, dict), "explicit_controller_required")
    supervisor = expected_process(owner["supervisor"])
    debugger = expected_process(owner["debugger"])
    require(len({expected["pid"], supervisor["pid"], debugger["pid"]}) == 3,
            "controller_processes_not_distinct")
    state_path = canonical_path(owner["state_path"])
    result_path = canonical_path(owner["result_path"])
    trace = canonical_path(owner["trace_dir"], directory=True)
    require(state_path == app.parent / "resident-controller.json"
            and result_path == trace / "debugger-result.json", "controller_paths_mismatch")
    maximum_age = owner.get("max_heartbeat_age_seconds")
    require(type(maximum_age) in (int, float) and math.isfinite(maximum_age)
            and 0 < maximum_age <= 8, "invalid_heartbeat_limit")
    kernel = kernel if kernel is not None else MacKernel()
    target_identity = check_process(expected, kernel)
    check_process(supervisor, kernel)
    debugger_identity = check_process(debugger, kernel)
    require(debugger_identity.get("ppid") == supervisor["pid"], "controller_parent_mismatch")
    inspect_module(module)
    state = read_json(state_path)
    result = read_json(result_path)
    require(state.get("resident") is True and state.get("phase") == "debugger_started"
            and state.get("supervisor_pid") == supervisor["pid"]
            and state.get("debugger_pid") == debugger["pid"]
            and state.get("trace_dir") == str(trace), "controller_state_mismatch")
    require(result.get("resident") is True and result.get("observation_state") == "active"
            and result.get("pid") == target_identity["pid"], "controller_target_mismatch")
    stamp = result.get("heartbeat_at_unix")
    current_time = time.time() if now is None else now
    require(type(stamp) in (int, float) and math.isfinite(stamp)
            and 0 <= current_time - stamp <= maximum_age, "controller_heartbeat_stale")
    # Re-read kernel identities after disk work; PID reuse is never rebound.
    check_process(expected, kernel)
    check_process(supervisor, kernel)
    require(check_process(debugger, kernel).get("ppid") == supervisor["pid"],
            "controller_parent_mismatch")
    for path in (profile_file, executable, module, info_path, state_path, result_path):
        canonical_path(str(path))
    identity_fields = ("pid", "uid", "start_seconds", "start_microseconds", "executable_path")
    return VerifiedTarget(expected["pid"], expected["uid"], expected["start_seconds"],
                          expected["start_microseconds"], str(app), str(executable), str(module),
                          ARM64_UUID, ARM64_SHA256, supervisor["pid"], debugger["pid"],
                          {key: supervisor[key] for key in identity_fields},
                          {key: debugger[key] for key in identity_fields})


def with_verified_target(profile_path, operation, *, kernel=None, now=None):
    """Call a caller-supplied operation only after complete preflight succeeds."""
    target = preflight(profile_path, kernel=kernel, now=now)
    return operation(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Explicit local private profile; no discovery")
    args = parser.parse_args()
    try:
        result = preflight(args.profile)
    except PreflightError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, "target": result.as_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
