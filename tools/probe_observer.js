// Generated settings are prepended by probe_run.py. No send RPCs or native calls.
'use strict';
function check(ok, reason) { if (!ok) throw new Error(reason); }
function readable(p, count) {
    if (p.isNull() || count < 0 || count > 8192) return false;
    const range = Process.findRangeByAddress(p);
    return range !== null && range.protection[0] === 'r' &&
        p.add(count).compare(range.base.add(range.size)) <= 0;
}
function hex(p, count) {
    return Array.from(new Uint8Array(p.readByteArray(count)), v => v.toString(16).padStart(2, '0')).join('');
}
function stdString(p, maximum) {
    if (!readable(p, 24)) return null;
    const flag = p.add(23).readU8();
    let data = p, length = flag;
    if (flag & 128) {
        data = p.readPointer();
        length = p.add(8).readU64().toNumber();
    }
    if (length > maximum || (!(flag & 128) && length > 22) || !readable(data, length)) return null;
    return data.readUtf8String(length);
}
check(Process.id === SETTINGS.pid && Process.arch === 'arm64', 'process mismatch');
check(Process.mainModule.path === SETTINGS.executable_path, 'executable mismatch');
const targetModule = Process.getModuleByName('wechat.dylib');
check(targetModule.path === SETTINGS.module_path, 'module path mismatch');
check(readable(targetModule.base, 32), 'unreadable Mach-O header');
check(targetModule.base.readU32() === 0xfeedfacf, 'Mach-O mismatch');
let cursor = targetModule.base.add(32), loadedUUID = null;
const commands = targetModule.base.add(16).readU32();
const commandBytes = targetModule.base.add(20).readU32();
check(commandBytes >= commands * 8 && commandBytes <= 65536, 'load command extent');
const commandEnd = cursor.add(commandBytes);
check(commands > 0 && commands < 200, 'load command count');
for (let i = 0; i < commands; i++) {
    check(cursor.add(8).compare(commandEnd) <= 0 && readable(cursor, 8), 'load command header');
    const command = cursor.readU32(), size = cursor.add(4).readU32();
    check(size >= 8 && size < 65536 && cursor.add(size).compare(commandEnd) <= 0,
        'load command size');
    if (command === 0x1b) {
        check(size === 24 && readable(cursor, 24) && loadedUUID === null, 'UUID command');
        loadedUUID = hex(cursor.add(8), 16);
    }
    cursor = cursor.add(size);
}
check(cursor.equals(commandEnd), 'load command trailing extent');
check(loadedUUID === '9ceb6ccf81a53b7f8fb5b4256fe67eeb', 'loaded UUID mismatch');
const finalizer = targetModule.base.add(0x4b573b8);
check(hex(finalizer, 12) === '086049391f0500712008407a', 'finalizer signature mismatch');
send({kind: 'metadata_verified', pid: Process.id, uuid: loadedUUID, receive: SETTINGS.observe});
if (SETTINGS.observe) {
    check(SETTINGS.conversation === 'filehelper', 'only filehelper experiment is implemented');
    check(/^WECHAT_AI_PROBE_[a-f0-9]{16}:$/.test(SETTINGS.prefix), 'invalid test prefix');
    const started = Math.floor(Date.now() / 1000), seen = new Set();
    let accepted = 0, errors = 0;
    const counters = {entry: 0, mode1: 0, message: 0, text: 0, fresh: 0,
        unique: 0, filehelper: 0, nonce: 0};
    const pending = [];
    setInterval(() => {
        while (pending.length) send(pending.shift());
    }, 50);
    // Aggregate stages only: no unrelated text, IDs, senders or timestamps.
    // A zero-sample observation must not be mistaken for a proven receiver.
    setInterval(() => send({kind: 'observer_counters', counters: {...counters}}), 5000);
    const listener = Interceptor.attach(finalizer, {
        onEnter(args) {
            try {
                counters.entry++;
                if (args[1].toInt32() !== 1 || accepted >= 100) return;
                counters.mode1++;
                const object = args[0];
                if (!readable(object, 0x168) || !object.readPointer().equals(targetModule.base.add(0x9cdb068))) return;
                counters.message++;
                if (object.add(0xc).readU32() !== 1) return;
                counters.text++;
                const created = object.add(0x114).readU32();
                if (created < started - 2 || created > Math.floor(Date.now()/1000) + 30) return;
                counters.fresh++;
                const id = object.add(0xf8).readU64().toString();
                if (id === '0' || seen.has(id)) return;
                counters.unique++;
                const from = stdString(object.add(0x18), 256), to = stdString(object.add(0x30), 256);
                if (from !== SETTINGS.conversation && to !== SETTINGS.conversation) return;
                counters.filehelper++;
                const body = stdString(object.add(0x130), 4096);
                if (body === null || !body.startsWith(SETTINGS.prefix)) return;
                counters.nonce++;
                seen.add(id); accepted++;
                pending.push({kind: 'sample', id, created, from, to, body, source: 'finalizer_mode_1',
                    coverage: 'filehelper_sync_not_true_inbound'});
            } catch (error) {
                errors++;
                if (errors <= 3) pending.push({kind: 'read_error', reason: String(error)});
                if (errors >= 3) listener.detach();
            }
        }
    });
    send({kind: 'observer_ready', maximum: 100, sends_enabled: false});
}
