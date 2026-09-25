// Runs the real DJPlusAI.js mapping under Node with a fake Mixxx `engine` and
// `midi`, driven by line-delimited JSON on stdin:
//   {"frame": [..sysex bytes..]}        -> DJPlusAI.incomingData(frame)
//   {"tick": n}                          -> fire every timer n times
//   {"set": [group, key, value]}         -> change a control behind the script's back
//   {"dump": true}                       -> print the control store
// Every SysEx the script sends is printed as {"out": [..bytes..]}.
"use strict";
const fs = require("fs");
const vm = require("vm");
const readline = require("readline");

const controls = {};
const timers = {};
let nextTimer = 1;
const out = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

const engine = {
    getValue: (g, k) => controls[g + "," + k] ?? 0,
    setValue: (g, k, v) => {
        controls[g + "," + k] = v;
        // Emulate LoopingControl::slotReloopToggle for loop tests.
        if (k === "reloop_toggle" && v > 0) {
            const on = controls[g + ",loop_enabled"] ?? 0;
            const s = controls[g + ",loop_start_position"] ?? -1;
            const e = controls[g + ",loop_end_position"] ?? -1;
            if (on) {
                controls[g + ",loop_enabled"] = 0;
            } else if (s >= 0 && e >= s) {
                controls[g + ",loop_enabled"] = 1;
            }
        }
    },
    beginTimer: (ms, fn) => {
        const id = nextTimer++;
        timers[id] = fn;
        return id;
    },
    stopTimer: (id) => {
        delete timers[id];
    },
    getPlayer: (g) => ({artist: "Björk " + g, title: "Jóga", key: "8A"}),
};
const midi = {sendSysexMsg: (bytes, len) => out({out: bytes.slice(0, len)})};

const ctx = vm.createContext({engine, midi, console, JSON, Math, String});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\nthis.DJPlusAI = DJPlusAI;", ctx);
const DJ = ctx.DJPlusAI;
DJ.init("DJPlusAI", false);

const rl = readline.createInterface({input: process.stdin});
rl.on("line", (line) => {
    if (!line.trim()) {
        return;
    }
    const cmd = JSON.parse(line);
    if (cmd.frame) {
        DJ.incomingData(new Uint8Array(cmd.frame), cmd.frame.length);
    }
    if (cmd.tick) {
        for (let i = 0; i < cmd.tick; i++) {
            for (const id of Object.keys(timers)) {
                if (timers[id]) {
                    timers[id]();
                }
            }
        }
    }
    if (cmd.set) {
        controls[cmd.set[0] + "," + cmd.set[1]] = cmd.set[2];
    }
    if (cmd.dump) {
        out({controls, timers: Object.keys(timers).length});
    }
    out({done: true});
});
