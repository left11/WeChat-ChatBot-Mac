#!/usr/bin/env bash
# Reuse the pinned devkit fetched by build_bridge.sh. Never launches WeChat.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${BRIDGE_CACHE_DIR:-/tmp/chatbot-bridge-cache}"
DEVKIT="${CACHE}/frida-core-devkit-17.8.0-macos-arm64"
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo 'Requires an Apple Silicon Mac.' >&2; exit 1; }
[[ -f "${DEVKIT}/frida-core.h" && -f "${DEVKIT}/libfrida-core.a" ]] || {
  echo 'Build bridge first to fetch and verify the pinned Frida devkit.' >&2; exit 1;
}
mkdir -p "${ROOT}/.probe-local/bin"
VALIDATOR_LITERAL="$(/usr/bin/python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "${ROOT}/tools/probe_target.py")"
xcrun clang -O1 -Wall -Wextra -Werror "${ROOT}/tools/probe_frida_host.c" \
  "-DPROBE_VALIDATOR_PATH=${VALIDATOR_LITERAL}" \
  -o "${ROOT}/.probe-local/bin/probe-frida-host" -I"${DEVKIT}" -L"${DEVKIT}" \
  -lfrida-core -lbsm -ldl -lm -lresolv -framework Foundation -framework AppKit \
  -framework IOKit -framework Security -lpthread
echo 'Built .probe-local/bin/probe-frida-host; no process attached.'
