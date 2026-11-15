# Desktop assistant engineering

This repository is a long-term fork of [Gusion1017/WeChat-ChatBot-Mac](https://github.com/Gusion1017/WeChat-ChatBot-Mac), maintained at [left11/WeChat-ChatBot-Mac](https://github.com/left11/WeChat-ChatBot-Mac). Keep the upstream [GPL license](../LICENSE) and existing attribution. The initial adaptation target is an isolated test-account Probe running `4.1.15.35` / build `270115`; this target is experimental until local receive and send checks establish support.

## Milestones

| Milestone | Deliverable | Evidence required |
| --- | --- | --- |
| M1: transport | Structured new text events and bounded automatic text replies on the test Probe | Exact message, sender, conversation and direction mapping; independently observed replies; no duplicate or wrong-conversation sends |
| M2: assistant | Summaries, todos and suggested replies | Grounded output from selected conversations and explicit user control over sending |
| M3: task dispatch | GPT/WorkBuddy task integration | Verified provider interface, explicit task scope, observable results and bounded permissions |

M1 does not promise full-history extraction, media support or general account compatibility. First prove a deterministic transport responder, then label an actual model-generated reply separately when a configured provider is available. A bounded supervised run is evidence for that run, not a long-term stability guarantee. Reuse the existing application services for M2/M3 after transport works.

## Where work runs

| Environment | Responsibilities | Limits |
| --- | --- | --- |
| Local Mac | Swift/AppKit and Darwin/CGO builds, synthetic LLDB/Frida fixture, native ABI validation, supervised Probe integration | Explicit test target only; one live operator |
| GitHub | Source, synthetic fixtures, reviewed changes, CI and upstream history | No account data, private profiles, native WeChat binaries or raw runtime evidence |
| Codex Cloud | Portable standard-library tests, static analysis, mocks, implementation and review using the attached fork | No live WeChat, native macOS certification, or unattended desktop service |

Cloud is an additional development lane. This work has not created or attached a dedicated Codex Cloud environment yet. Connecting the fork and running its documented portable checks establishes the Cloud environment; creating a workflow file alone does not. Model credentials and real messages are not required for offline development.

## Probe operating contract

Use a private profile in `.probe-local/` containing the approved canonical app/executable location, expected bundle identity, full version/build, module fingerprint and current process/controller identity. Public examples use synthetic placeholders. Never infer the target from a process name, generic application discovery, a remembered PID or a default network port. Recheck identity before each runtime operation and stop on changes.

The dedicated minimal Probe runner is being developed. There is no supported Probe launch command in this guide yet. The upstream `tools/manage_wechat.sh`, `tools/install_gadget.sh`, `scripts/run_bridge.sh` and application auto-discovery are not approved entry points for this existing Probe. Do not use their defaults, installation, restore or relaunch paths to work around missing Probe capabilities.

The existing Probe has an LLDB owner and a URL-specific bridge. Leave that controller and its URL service intact. No second debugger, owner detach/kill, generic URL command channel or replacement resident library is allowed. Frida attachment can change process state even when its transport uses Mach rather than a debugger interface.

Progress through these gates in order:

1. Prove strict target rejection with synthetic profiles and operation spies. Wrong, missing, stale and ambiguous identities must produce zero runtime operations.
2. Verify native symbol/layout candidates offline. Record provenance and separate candidate, static verification and runtime verification. Do not enable features by copying offsets or changing only a version label.
3. In a separate no-data test process, validate the relevant LLDB ownership/breakpoint behavior with Frida attachment, minimal instrumentation, cleanup and failure paths. A passing fixture proves the mechanism only.
4. Revalidate Probe identity and owner, then perform a bounded receive-only experiment through the reviewed integration route. Disable unrelated media, contact, database and send hooks.
5. Verify one controlled send, then bounded automatic replies in the configured test conversation. Require stable event deduplication, self-event rejection and one in-flight send. Submission success alone is not delivery evidence; hold uncertain outcomes for reconciliation.

If a gate fails, preserve local evidence and stop the new operation. Do not recover by acting on the daily client, changing the existing owner or retrying an uncertain send. Only cleanup already shown safe for the mechanism may run against Probe. Incoming text cannot authorize shell commands, change an allowlist or dispatch WorkBuddy tasks.

## Development checks

Run these from the repository root without account credentials or a running WeChat:

```sh
bash scripts/check.sh --docs
python3 -m unittest discover -s tools -p 'test_probe_*.py' -v
```

The Ubuntu workflow also checks shell/Python syntax and rejects tracked runtime artifacts. Probe tests use the Python standard library and synthetic inputs; they must not attach, launch, send, inspect live account stores or access the network. Keep optional platform-specific integrations out of this portable suite.

On a suitable local Mac, the existing native check is:

```sh
bash scripts/check.sh --with-go
```

If Command Line Tools lacks the required Swift macro plugins, select a compatible full Xcode toolchain for this command only. Adjust the conventional Xcode installation path if needed; do not change the system-wide selection:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer bash scripts/check.sh --with-go
```

Inspect `scripts/build_bridge.sh` before running: it builds the Darwin bridge and may download the pinned toolchain/development kit. The initial fork's Swift baseline passed 933 XCTest cases with full Xcode after a Command Line Tools macro-plugin failure. That result establishes the existing Swift baseline, not native Hook compatibility or live receive/send. `CHECK_SKIP_SWIFT=1` skips Swift tests and must be reported as skipped coverage, not a native pass. Native build/test commands do not authorize installation or live Probe operations.

## Evidence, changes and upstream updates

Keep raw observations, private profiles and ABI evidence under `.probe-local/`, and local plans/state under `.omx/`. Commit only synthetic examples and sanitized conclusions with build identity, sample counts, elapsed observation time and known gaps. Do not attach screenshots, logs, account IDs, chats, memory/database exports, access tokens or private filesystem paths to public PRs or CI artifacts. Ignore rules are a backstop; inspect the staged diff before every push.

Use `origin` for this fork and `upstream` for the original repository. Develop changes on focused branches, preserve the upstream baseline/history and submit reviewable commits. Upstream syncs require review of target selection, injection, default version tables and send behavior before local integration; a merge must not broaden the Probe allowlist or silently enable a new build. Preserve existing hook/bridge script-copy checks when touching their shared source.

Record why a change was made and its constraints in the commit body with Lore trailers. Include tested commands, unavailable native/live coverage and explicit follow-up gates. A feature becomes supported only when its own dynamic evidence is available; source compilation, a static address match and an HTTP success are separate claims.

## Current validation status

The native Swift baseline passed 933 XCTest cases with full Xcode, and the pinned Go/Frida bridge build and Go tests passed. The 20 synthetic Probe identity tests, static address validation and a read-only live identity preflight passed. No real Probe message observer has been attached and no message has been sent by this fork. The diagnostic Frida host and receive script are experimental components, not an operational Probe runner.

Direct Frida attachment timed out in a synthetic program both with and without an LLDB owner, including a target signed with the four Probe entitlements. The LLDB hardware breakpoint continued to work. A separate same-UID C caller received Mach error 5 from `task_for_pid`; this is evidence about that caller, not a proven root cause for the Frida timeout. No security setting or daily WeChat instance was changed. Runtime support remains unverified. Startup-loaded Gadget also failed the synthetic tests: the untraced process was killed with CODESIGNING Invalid Page during Gadget initialization, while the hardware-LLDB launch did not finish within its external deadline. Neither route is approved for the live Probe. A possible next diagnostic is a separately authorized, temporary developer-debugging policy change, followed by the same synthetic checks and restoration of the original setting; it is not a proven fix.

The initial fork change also passed [GitHub Actions](https://github.com/left11/WeChat-ChatBot-Mac/actions/runs/43725236956). This is portable CI evidence only. All temporary diagnostic processes were cleaned up; no production account state or system security policy was changed.
