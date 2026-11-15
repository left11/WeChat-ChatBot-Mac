#!/usr/bin/env python3
"""Verify experimental 270115 anchors from an explicitly supplied disk module.

Uses probe_target for the pinned binary identity. This tool has no process,
attach, launch, account-data, or network operations and writes no files.
"""

import argparse
import json
from pathlib import Path
import struct
import sys

from probe_target import ARM64_SHA256, ARM64_UUID, PreflightError, inspect_module, require

MANIFEST = Path(__file__).resolve().parents[1] / "hook/experimental/4_1_15_35_mac.json"


class DiskImage:
    """Only map file-backed segments after the shared identity check passed."""

    def __init__(self, path):
        def identity():
            info = path.stat()
            # Reading may legitimately update atime; compare content identity.
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

        original = identity()
        inspect_module(path)
        self.data = path.read_bytes()
        require(identity() == original, "module_changed")
        count = struct.unpack_from(">I", self.data, 4)[0]
        slices = (struct.unpack_from(">IIIII", self.data, 8 + i * 20) for i in range(count))
        self.base = next(offset for cpu, _, offset, _, _ in slices if cpu == 0x100000C)
        position = self.base + 32
        self.segments = []
        for _ in range(struct.unpack_from("<I", self.data, self.base + 16)[0]):
            command, length = struct.unpack_from("<II", self.data, position)
            if command == 0x19:
                address, _, offset, size = struct.unpack_from("<QQQQ", self.data, position + 24)
                self.segments.append((address, self.base + offset, size))
            position += length

    def read(self, address, size):
        for vmaddr, fileoff, filesize in self.segments:
            offset = address - vmaddr
            if 0 <= offset and offset + size <= filesize:
                return self.data[fileoff + offset:fileoff + offset + size]
        raise PreflightError("anchor_not_file_backed")

    def instruction(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def adrp(self, address):
        word = self.instruction(address)
        require(word & 0x9F000000 == 0x90000000, "adrp_anchor_mismatch")
        immediate = ((word >> 29) & 3) | (((word >> 5) & 0x7FFFF) << 2)
        if immediate & (1 << 20):
            immediate -= 1 << 21
        return (address & ~0xFFF) + (immediate << 12)

    def add_address(self, page_instruction, add_instruction):
        word = self.instruction(add_instruction)
        require(word & 0xFFC00000 == 0x91000000, "add_anchor_mismatch")
        return self.adrp(page_instruction) + ((word >> 10) & 0xFFF)

    def pointer(self, address):
        # This fixed arm64 image uses chained rebase encodings. These masked
        # targets are static evidence only; never use them as live pointers.
        return struct.unpack("<Q", self.read(address, 8))[0] & 0xFFFFFFFFF

    def call_target(self, address):
        word = self.instruction(address)
        require(word & 0xFC000000 == 0x94000000, "call_anchor_mismatch")
        immediate = word & 0x3FFFFFF
        if immediate & (1 << 25):
            immediate -= 1 << 26
        return address + immediate * 4


def verify(module, manifest):
    require(manifest["runtime_verified"] is False and manifest["experimental"] is True
            and manifest["enableSend"] is False and manifest["req2bufSendMode"] == "off"
            and manifest["r3Slot13Capture"] is False and manifest["r3ClosureCapture"] is False,
            "experimental_map_must_remain_disabled")
    require(manifest["arm64UUID"] == ARM64_UUID
            and manifest["arm64SHA256"] == ARM64_SHA256, "manifest_identity_mismatch")
    image = DiskImage(module)

    def address(name):
        return int(manifest[name], 16)

    def expect(actual, expected):
        require(actual == expected, "static_anchor_mismatch")

    for name, prefix in {
        "messageFinalizerAddr": "086049391f0500712008407a",
        "stringCtorAddr": "f85fbca9f65701a9f44f02a9fd7b03a9",
        "serviceScopeAddr": "ff4306d1fc6f15a9f65716a9f44f17a9",
        "slot13Addr": "fa67bba9f85f01a9f65702a9f44f03a9",
        "newSendMsgTaskCtorAddr": "f657bda9f44f01a9fd7b02a9fd830091",
        "operatorNewAddr": "504d01d010a245f900021fd6",
    }.items():
        expect(image.read(address(name), len(prefix) // 2).hex(), prefix)
    for call, name in ((0x4B565D0, "messageFinalizerAddr"), (0x57ED528, "stringCtorAddr"),
                       (0x57ED530, "serviceScopeAddr"), (0x57ED550, "sendFuncAddr"),
                       (0x2F88748, "newSendMsgTaskCtorAddr")):
        expect(image.call_target(call), address(name))
    for page, add, name in ((0x4B5614C, 0x4B56150, "messageVtable"),
                            (0x44CCD24, 0x44CCD28, "classAVtable"),
                            (0x2F888D0, 0x2F888D4, "newSendMsgTaskVtable"),
                            (0x2F884D8, 0x2F884DC, "r3ClosureVtable")):
        expect(image.add_address(page, add), address(name))
    expect(image.pointer(address("classAVtable") + 0x68), address("slot13Addr"))
    expect(image.pointer(0x9CBA650 + 0x28), 0x44D4074)
    expect(image.read(0x44D4074, 8).hex(), "004000d1ccfcff17")
    expect(image.add_address(0x4506198, 0x450619C), 0x9CBCCB0)
    expect(image.pointer(0x9CBCCB0 + 0x18), 0x45098E0)
    expect(image.read(0x45098E0, 8).hex(), "002841f9c0035fd6")
    expect(image.adrp(0x4508D3C) + 0x168, address("globalServiceHolder"))
    expect(image.adrp(0x4510688) + 0x168, address("globalServiceHolder"))
    expect(image.pointer(address("r3ClosureVtable") + 0x10), 0x2F88C64)
    expect(image.add_address(0x2F88C7C, 0x2F88C80), address("r3ClosureVtable"))
    expect(image.read(address("defaultScopeString"), 8), b"default\0")
    fragments = (0x85D67F8, 0x85D6800, 0x85D6870, 0x85D6878,
                 0x85D6880, 0x868D240, 0x86F43D8, 0x86F43E0)
    url = b"".join(image.read(part, 4) for part in fragments).rstrip(b"\0").decode()
    expect(url, "/cgi-bin/micromsg-bin/newsendmsg")
    expect(url, manifest["newsendmsgUrl"])
    expect(image.read(0x86F43F0, 16).hex(), "0a020000000000000000000001000000")
    expect(image.read(0x86F43D0, 8).hex(), "0a02000000000000")
    return {"status": "static_anchors_verified", "runtime_verified": False,
            "enableSend": False, "arm64_uuid": ARM64_UUID, "arm64_sha256": ARM64_SHA256,
            "scope": ["receive_finalizer", "text_descriptor", "text_task", "text_closure", "service_submit"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, type=Path,
                        help="Explicit path to the isolated Probe's disk wechat.dylib")
    args = parser.parse_args()
    try:
        result = verify(args.module, json.loads(MANIFEST.read_text()))
    except PreflightError as error:
        print(json.dumps({"status": "rejected", "error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, struct.error, StopIteration):
        print(json.dumps({"status": "rejected", "error": "invalid_static_input"}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
