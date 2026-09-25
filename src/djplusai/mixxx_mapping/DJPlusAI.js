// DJPlusAI bridge controller script for Mixxx (2.4+).
//
// Receives JSON commands wrapped in SysEx frames from the djplusai Python
// bridge, executes them against Mixxx's control objects and streams deck
// state back the same way. See src/djplusai/protocol.py for the frame format.
//
// Frame: F0 7D 44 4A <dir> <ascii json> F7   (dir 01 = to Mixxx, 02 = from Mixxx)

var DJPlusAI = {
    VERSION: "0.1.0",
    HEADER: [0xF0, 0x7D, 0x44, 0x4A],
    TO_MIXXX: 0x01,
    FROM_MIXXX: 0x02,
    numDecks: 4,
    pushIntervalMs: 50,
    // Decks that are not playing are reported on every Nth push only.
    idleEvery: 10,
    rampIntervalMs: 20,
    ramps: {},
    rampTimer: 0,
    pushTimer: 0,
    tick: 0,
    lastMeta: {},
};

DJPlusAI.DECK_KEYS = [
    "play", "playposition", "duration", "bpm", "file_bpm", "rate", "volume",
    "pregain", "loop_enabled", "loop_start_position", "loop_end_position",
    "beat_distance", "track_loaded", "track_samplerate", "sync_enabled",
    "quantize", "keylock", "key", "pfl", "orientation",
];

DJPlusAI.init = function(_id, _debugging) {
    DJPlusAI.pushTimer = engine.beginTimer(DJPlusAI.pushIntervalMs, DJPlusAI.pushState);
    DJPlusAI.send({t: "hello", version: DJPlusAI.VERSION, decks: DJPlusAI.numDecks});
};

DJPlusAI.shutdown = function() {
    if (DJPlusAI.pushTimer) {
        engine.stopTimer(DJPlusAI.pushTimer);
        DJPlusAI.pushTimer = 0;
    }
    if (DJPlusAI.rampTimer) {
        engine.stopTimer(DJPlusAI.rampTimer);
        DJPlusAI.rampTimer = 0;
    }
    DJPlusAI.send({t: "bye"});
};

// ---------------------------------------------------------------- encoding

DJPlusAI.toAsciiJson = function(obj) {
    // SysEx payload bytes must be < 0x80: escape everything else as \uXXXX.
    return JSON.stringify(obj).replace(/[\u0080-￿]/g, function(c) {
        return "\\u" + ("0000" + c.charCodeAt(0).toString(16)).slice(-4);
    });
};

DJPlusAI.send = function(obj) {
    var text = DJPlusAI.toAsciiJson(obj);
    var bytes = DJPlusAI.HEADER.concat([DJPlusAI.FROM_MIXXX]);
    for (var i = 0; i < text.length; i++) {
        bytes.push(text.charCodeAt(i));
    }
    bytes.push(0xF7);
    midi.sendSysexMsg(bytes, bytes.length);
};

DJPlusAI.decode = function(data, length) {
    var n = length || data.length;
    if (n < 7) {
        return null;
    }
    for (var h = 0; h < DJPlusAI.HEADER.length; h++) {
        if (data[h] !== DJPlusAI.HEADER[h]) {
            return null;
        }
    }
    // Ignore our own frames echoed back by loopback ports.
    if (data[4] !== DJPlusAI.TO_MIXXX) {
        return null;
    }
    var end = data[n - 1] === 0xF7 ? n - 1 : n;
    var text = "";
    for (var i = 5; i < end; i++) {
        text += String.fromCharCode(data[i]);
    }
    try {
        return JSON.parse(text);
    } catch (e) {
        DJPlusAI.send({t: "error", err: "bad json: " + e});
        return null;
    }
};

// ---------------------------------------------------------------- commands

DJPlusAI.incomingData = function(data, length) {
    var msg = DJPlusAI.decode(data, length);
    if (msg === null) {
        return;
    }
    var reply = {t: "reply", id: msg.id, ok: true};
    try {
        var result = DJPlusAI.execute(msg);
        if (result !== undefined) {
            reply.v = result;
        }
    } catch (e) {
        reply.ok = false;
        reply.err = String(e);
    }
    if (msg.id !== undefined) {
        DJPlusAI.send(reply);
    }
};

DJPlusAI.execute = function(msg) {
    switch (msg.op) {
    case "hello":
        return {version: DJPlusAI.VERSION, decks: DJPlusAI.numDecks};
    case "get":
        return engine.getValue(msg.g, msg.k);
    case "set":
        engine.setValue(msg.g, msg.k, msg.v);
        return engine.getValue(msg.g, msg.k);
    case "press":
        // Momentary button press, e.g. LoadSelectedTrack or reloop_toggle.
        engine.setValue(msg.g, msg.k, 1);
        engine.setValue(msg.g, msg.k, 0);
        return undefined;
    case "batch":
        var out = [];
        for (var i = 0; i < msg.ops.length; i++) {
            out.push(DJPlusAI.execute(msg.ops[i]));
        }
        return out;
    case "loop":
        return DJPlusAI.setLoop(msg.g, msg.s, msg.e, msg.enable);
    case "ramp":
        DJPlusAI.startRamp(msg.g, msg.k, msg.to, msg.ms, msg.curve);
        return undefined;
    case "cancel_ramps":
        DJPlusAI.cancelRamps(msg.g);
        return undefined;
    case "state":
        return DJPlusAI.fullState();
    case "config":
        if (msg.decks) {
            DJPlusAI.numDecks = msg.decks;
        }
        if (msg.interval && msg.interval >= 20) {
            DJPlusAI.pushIntervalMs = msg.interval;
            engine.stopTimer(DJPlusAI.pushTimer);
            DJPlusAI.pushTimer = engine.beginTimer(DJPlusAI.pushIntervalMs, DJPlusAI.pushState);
        }
        return {decks: DJPlusAI.numDecks, interval: DJPlusAI.pushIntervalMs};
    default:
        throw "unknown op " + msg.op;
    }
};

// Arm a loop at exact sample positions. When the loop lies ahead of the play
// position Mixxx keeps playing normally and engages it sample-accurately when
// the playhead gets there - so lyric-cued loops don't depend on MIDI latency.
DJPlusAI.setLoop = function(group, start, end, enable) {
    if (engine.getValue(group, "loop_enabled")) {
        DJPlusAI.execute({op: "press", g: group, k: "reloop_toggle"});
    }
    engine.setValue(group, "loop_end_position", -1);
    engine.setValue(group, "loop_start_position", start);
    engine.setValue(group, "loop_end_position", end);
    if (enable) {
        DJPlusAI.execute({op: "press", g: group, k: "reloop_toggle"});
    }
    return [
        engine.getValue(group, "loop_start_position"),
        engine.getValue(group, "loop_end_position"),
        engine.getValue(group, "loop_enabled"),
    ];
};

// ---------------------------------------------------------------- ramps

DJPlusAI.startRamp = function(group, key, to, ms, curve) {
    var from = engine.getValue(group, key);
    DJPlusAI.ramps[group + "|" + key] = {
        g: group, k: key, from: from, to: to,
        ms: Math.max(ms, 1), elapsed: 0, curve: curve || "linear",
    };
    if (!DJPlusAI.rampTimer) {
        DJPlusAI.rampTimer = engine.beginTimer(DJPlusAI.rampIntervalMs, DJPlusAI.stepRamps);
    }
};

// Cancels ramps on `group` and on groups nested in it, e.g. "[Channel1]" also
// covers "[EqualizerRack1_[Channel1]_Effect1]" and "[QuickEffectRack1_[Channel1]]".
DJPlusAI.cancelRamps = function(group) {
    for (var id in DJPlusAI.ramps) {
        if (!group || DJPlusAI.ramps[id].g.indexOf(group) !== -1) {
            delete DJPlusAI.ramps[id];
        }
    }
};

DJPlusAI.shape = function(x, curve) {
    if (curve === "scurve") {
        return x * x * (3 - 2 * x);
    }
    if (curve === "exp") {
        return x * x;
    }
    if (curve === "log") {
        return Math.sqrt(x);
    }
    return x;
};

DJPlusAI.stepRamps = function() {
    var active = 0;
    for (var id in DJPlusAI.ramps) {
        var r = DJPlusAI.ramps[id];
        r.elapsed += DJPlusAI.rampIntervalMs;
        var x = Math.min(r.elapsed / r.ms, 1);
        engine.setValue(r.g, r.k, r.from + (r.to - r.from) * DJPlusAI.shape(x, r.curve));
        if (x >= 1) {
            delete DJPlusAI.ramps[id];
        } else {
            active++;
        }
    }
    if (!active && DJPlusAI.rampTimer) {
        engine.stopTimer(DJPlusAI.rampTimer);
        DJPlusAI.rampTimer = 0;
    }
};

// ---------------------------------------------------------------- state push

DJPlusAI.deckGroup = function(n) {
    return "[Channel" + n + "]";
};

DJPlusAI.deckState = function(n) {
    var g = DJPlusAI.deckGroup(n);
    var s = {};
    for (var i = 0; i < DJPlusAI.DECK_KEYS.length; i++) {
        s[DJPlusAI.DECK_KEYS[i]] = engine.getValue(g, DJPlusAI.DECK_KEYS[i]);
    }
    var eq = "[EqualizerRack1_" + g + "_Effect1]";
    s.eq = [
        engine.getValue(eq, "parameter1"),
        engine.getValue(eq, "parameter2"),
        engine.getValue(eq, "parameter3"),
    ];
    s.filter = engine.getValue("[QuickEffectRack1_" + g + "]", "super1");
    return s;
};

DJPlusAI.deckMeta = function(n) {
    // engine.getPlayer() only exists in newer Mixxx versions.
    if (typeof engine.getPlayer !== "function") {
        return null;
    }
    try {
        var p = engine.getPlayer(DJPlusAI.deckGroup(n));
        if (!p) {
            return null;
        }
        return {artist: p.artist || "", title: p.title || "", key: p.key || ""};
    } catch (e) {
        return null;
    }
};

DJPlusAI.masterState = function() {
    return {
        crossfader: engine.getValue("[Master]", "crossfader"),
        gain: engine.getValue("[Master]", "gain"),
    };
};

DJPlusAI.fullState = function() {
    var decks = {};
    for (var n = 1; n <= DJPlusAI.numDecks; n++) {
        decks[n] = DJPlusAI.deckState(n);
        var meta = DJPlusAI.deckMeta(n);
        if (meta) {
            decks[n].meta = meta;
        }
    }
    return {decks: decks, master: DJPlusAI.masterState()};
};

DJPlusAI.pushState = function() {
    DJPlusAI.tick++;
    var full = DJPlusAI.tick % DJPlusAI.idleEvery === 0;
    for (var n = 1; n <= DJPlusAI.numDecks; n++) {
        var g = DJPlusAI.deckGroup(n);
        if (!full && !engine.getValue(g, "play")) {
            continue;
        }
        var state = DJPlusAI.deckState(n);
        var meta = DJPlusAI.deckMeta(n);
        var metaKey = meta ? meta.artist + "\u0000" + meta.title : "";
        if (meta && (full || DJPlusAI.lastMeta[n] !== metaKey)) {
            state.meta = meta;
            DJPlusAI.lastMeta[n] = metaKey;
        }
        DJPlusAI.send({t: "deck", n: n, s: state});
    }
    if (full) {
        DJPlusAI.send({t: "master", s: DJPlusAI.masterState()});
    }
};
