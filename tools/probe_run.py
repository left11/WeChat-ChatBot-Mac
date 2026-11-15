#!/usr/bin/env python3
"""Bounded, receive-only integration with one explicitly configured test Probe.

No discovery, application launch, restart, send, account-store access or policy
changes. Raw events stay in the ignored local run directory. The native host
independently repeats the same preflight immediately before attachment.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time

from probe_target import PreflightError, canonical_path, preflight, require

ROOT = Path(__file__).resolve().parents[1]


def settings_for(target, mode, prefix):
    require(mode in ("metadata", "observe"), "unsupported_observation_mode")
    return {"pid": target.pid, "executable_path": target.executable_path,
            "module_path": target.module_path, "observe": mode == "observe",
            "conversation": "filehelper", "prefix": prefix}


def check_same(profile, original, validator=preflight):
    require(validator(profile) == original, "target_generation_changed")


def inspect_log(path, mode, pid):
    counts = Counter()
    lifecycle = False
    mode_error = False
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("host_lifecycle") == "complete":
            lifecycle |= (event.get("cleanup_verified") is True
                          and event.get("metadata_ready") is True and event.get("ok") is True
                          and event.get("mode") == "probe" and event.get("pid") == pid)
        if event.get("type") == "error":
            counts["script_error"] += 1
        payload = event.get("payload")
        if event.get("type") == "send" and isinstance(payload, dict):
            kind = payload.get("kind")
            if isinstance(kind, str):
                counts[kind] += 1
            if kind == "metadata_verified":
                mode_error |= (payload.get("pid") != pid
                               or payload.get("receive") is not (mode == "observe"))
    if mode == "metadata":
        mode_error |= bool(counts["observer_ready"] or counts["sample"])
    ready = counts["metadata_verified"] == 1 and not mode_error
    if mode == "observe":
        ready &= counts["observer_ready"] == 1
    return {"counts": dict(counts), "ready": ready,
            "cleanup_verified": lifecycle,
            "mode_error": mode_error,
            "script_error": bool(counts["read_error"] or counts["script_error"] or mode_error)}


@contextmanager
def handle_termination():
    requested = False

    def terminate(_signum, _frame):
        nonlocal requested
        if not requested:
            requested = True
            raise PreflightError("runner_terminated")

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run(profile, mode, seconds, *, validator=preflight, spawn=subprocess.Popen):
    # SIGTERM must enter the same own-host cleanup as a validation failure.
    # Repeated termination requests cannot interrupt that cleanup.
    with handle_termination():
        return _run(profile, mode, seconds, validator=validator, spawn=spawn)


def _run(profile, mode, seconds, *, validator, spawn):
    require(type(seconds) is int and 1 <= seconds <= 300, "invalid_observation_duration")
    require(mode in ("metadata", "observe"), "unsupported_observation_mode")
    # Invalid/stale targets fail before even creating a host process.
    target = validator(profile)
    host = canonical_path(str(ROOT / ".probe-local/bin/probe-frida-host"))
    require(os.access(host, os.X_OK), "host_not_executable")
    private = ROOT / ".probe-local"
    private.mkdir(mode=0o700, exist_ok=True)
    require(not private.is_symlink(), "private_directory_symlink")
    lock_fd = os.open(private / "probe-run.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PreflightError("another_probe_run_owns_lock") from None
        check_same(profile, target, validator)
        nonce = secrets.token_hex(8)
        prefix = "WECHAT_AI_PROBE_" + nonce + ":"
        output = private / ("run-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + nonce)
        output.mkdir(mode=0o700)
        source = output / "observer.js"
        source.write_text("const SETTINGS = " + json.dumps(settings_for(target, mode, prefix)) + ";\n"
                          + (ROOT / "tools/probe_observer.js").read_text())
        source.chmod(0o600)
        report = {"mode": mode, "seconds": seconds, "prefix": prefix,
                  "sends_enabled": False, "pass": False, "events": {},
                  "target": target.as_dict(), "started_at": time.time()}
        process = None
        started = time.monotonic()
        log_path = output / "host.log"
        with log_path.open("x") as log:
            log_path.chmod(0o600)
            try:
                check_same(profile, target, validator)
                process = spawn([str(host), "--probe-profile", str(profile), str(source), str(seconds)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                print(json.dumps({"stage": "host_started", "run_dir": str(output),
                                  "mode": mode, "prefix": prefix, "sends_enabled": False}), flush=True)
                next_check = time.monotonic()
                announced = False
                while process.poll() is None:
                    require(time.monotonic() - started < seconds + 75, "host_deadline_exceeded")
                    if time.monotonic() >= next_check:
                        check_same(profile, target, validator)
                        next_check = time.monotonic() + 2
                        if not announced and inspect_log(log_path, mode, target.pid)["ready"]:
                            print(json.dumps({"stage": "observer_ready" if mode == "observe" else "metadata_ready",
                                              "prefix": prefix, "sends_enabled": False}), flush=True)
                            announced = True
                    time.sleep(0.1)
                report["host_returncode"] = process.returncode
            except (PreflightError, OSError, KeyboardInterrupt) as error:
                report["error"] = str(error) or type(error).__name__
            finally:
                if process is not None and process.poll() is None:
                    # This handle is solely our host, never Probe or its owner.
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        report["forced_host_exit_cleanup_unknown"] = True
                log.flush()
        report["events"] = inspect_log(log_path, mode, target.pid)
        try:
            check_same(profile, target, validator)
            report["postflight_passed"] = True
        except PreflightError as error:
            report["postflight_error"] = str(error)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["pass"] = (report.get("host_returncode") == 0 and not report.get("error")
                          and not report.get("forced_host_exit_cleanup_unknown")
                          and report.get("postflight_passed") is True
                          and report["events"]["ready"] and report["events"]["cleanup_verified"]
                          and not report["events"]["script_error"])
        result = output / "result.json"
        result.write_text(json.dumps(report, indent=2) + "\n")
        result.chmod(0o600)
        print(json.dumps({"stage": "completed", "pass": report["pass"],
                          "mode": mode, "events": report["events"],
                          "error": report.get("error"), "postflight": report.get("postflight_passed", False),
                          "result": str(result)}), flush=True)
        return 0 if report["pass"] else 1
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Absolute private profile; no discovery")
    parser.add_argument("--mode", required=True, choices=("metadata", "observe"))
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    try:
        return run(args.profile, args.mode, args.seconds)
    except (PreflightError, OSError) as error:
        print(json.dumps({"pass": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
