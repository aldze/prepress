/*
 * merge_spots.jsx  (Adobe Photoshop)
 * ----------------------------------
 * Сводит плашечные (spot) каналы растрового файла в CMYK и переводит
 * результат в целевой ICC-профиль. Результат пишется во ВРЕМЕННЫЙ TIFF,
 * дальше его подхватывает prepress.py и гонит через обычную обработку
 * (ресемплинг, шарпинг, запись итогового TIFF). Исходник не меняется.
 *
 * Почему Photoshop, а не ImageMagick: свести плашку — значит подмешать её
 * краску в CMYK по её же кривой и наложению. ImageMagick лишние каналы
 * просто выбросит, и краска пропадёт молча.
 *
 * Вызывается из prepress.py через AppleScript:
 *   tell application id "com.adobe.Photoshop"
 *       do javascript (POSIX file ".../merge_spots.jsx")
 *   end tell
 *
 * Параметры и результат — файлами (см. комментарий в collect_links.jsx):
 *   ~/Library/Application Support/prepress_automation/ps_params.json
 *   {"jobs":[{"src":..,"dst":..,"profile":"ISO coated ...",
 *             "intent":"RELATIVECOLORIMETRIC","blackPointCompensation":true}],
 *    "outPath":"...", "nonce":"..."}
 *
 * profile — НАЗВАНИЕ профиля внутри .icc (Photoshop адресует профили именно
 * так), его считает prepress.py через `magick -format %[icc:description]`.
 */

var __args = arguments;

var PARAMS_PATH = Folder.userData.fsName + "/prepress_automation/ps_params.json";

// --- служебное (в ExtendScript нет JSON) ----------------------------------

function readParams() {
    var f = new File(PARAMS_PATH);
    if (!f.exists) { return null; }
    f.encoding = "UTF-8";
    f.open("r");
    var txt = f.read();
    f.close();
    if (!txt) { return null; }
    return eval("(" + txt + ")");
}

function jsEsc(s) {
    s = String(s);
    var out = "";
    for (var i = 0; i < s.length; i++) {
        var c = s.charAt(i);
        var code = s.charCodeAt(i);
        if (c === '"') { out += '\\"'; }
        else if (c === '\\') { out += '\\\\'; }
        else if (code === 10) { out += '\\n'; }
        else if (code === 13) { out += '\\r'; }
        else if (code === 9) { out += '\\t'; }
        else if (code < 32 || code > 126) {
            var h = code.toString(16);
            while (h.length < 4) { h = "0" + h; }
            out += "\\u" + h;
        }
        else { out += c; }
    }
    return out;
}

function jsVal(v) {
    if (v === null || v === undefined) { return "null"; }
    var t = typeof v;
    if (t === "number") { return isFinite(v) ? String(v) : "null"; }
    if (t === "boolean") { return v ? "true" : "false"; }
    return '"' + jsEsc(v) + '"';
}

function jsObj(pairs) {
    var parts = [];
    for (var i = 0; i < pairs.length; i += 2) {
        parts.push('"' + jsEsc(pairs[i]) + '":' + jsVal(pairs[i + 1]));
    }
    return "{" + parts.join(",") + "}";
}

function safe(fn, fallback) {
    try {
        var v = fn();
        if (v === undefined) { return fallback; }
        return v;
    } catch (e) {
        return fallback;
    }
}

function intentByName(name) {
    name = String(name || "").toUpperCase();
    if (name === "PERCEPTUAL") { return Intent.PERCEPTUAL; }
    if (name === "SATURATION") { return Intent.SATURATION; }
    if (name === "ABSOLUTECOLORIMETRIC") { return Intent.ABSOLUTECOLORIMETRIC; }
    return Intent.RELATIVECOLORIMETRIC;
}

// --- один файл ------------------------------------------------------------

function processJob(job) {
    var warnings = [];
    var res = ["src", String(job.src), "dst", String(job.dst)];

    var src = new File(String(job.src));
    if (!src.exists) {
        return jsObj(res.concat(["ok", false, "error", "файл не найден на диске"]));
    }

    var doc = null;
    try {
        doc = app.open(src);
    } catch (e) {
        return jsObj(res.concat(["ok", false, "error", "Photoshop не открыл файл: " + e]));
    }

    try {
        var wasMode = safe(function () { return String(doc.mode); }, "");

        // Duotone и Multichannel — это сами по себе плашечные режимы; каналы
        // в них сводятся только переводом режима документа.
        if (safe(function () { return doc.mode === DocumentMode.DUOTONE; }, false)) {
            doc.changeMode(ChangeMode.CMYK);
            warnings.push("Duotone mode converted to CMYK using the Photoshop working space");
        } else if (safe(function () { return doc.mode === DocumentMode.MULTICHANNEL; }, false)) {
            var okm = safe(function () { doc.changeMode(ChangeMode.CMYK); return true; }, false);
            warnings.push(okm
                ? "режим Multichannel переведён в CMYK"
                : "режим Multichannel: перевести в CMYK не удалось");
        }

        // Плашечные каналы. Идём с конца: merge() удаляет канал из списка.
        var merged = [];
        var chans = safe(function () { return doc.channels; }, null);
        if (chans) {
            for (var i = chans.length - 1; i >= 0; i--) {
                var ch = chans[i];
                var kind = safe(function () { return ch.kind; }, null);
                if (kind !== ChannelType.SPOTCOLOR) { continue; }
                var nm = safe(function () { return ch.name; }, "без имени");
                var ok = safe(function () { ch.merge(); return true; }, false);
                if (ok) { merged.push(nm); }
                else { warnings.push("channel " + nm + ": could not be merged"); }
            }
        }

        // Слои сводим сами: иначе saveAs с layers=false свёл бы их молча,
        // а нам нужен предсказуемый композит — тот же, что берёт ImageMagick.
        if (safe(function () { return doc.layers.length; }, 1) > 1) {
            safe(function () { doc.flatten(); return true; }, false);
        }

        if (job.profile) {
            var conv = safe(function () {
                doc.convertProfile(String(job.profile), intentByName(job.intent),
                                   job.blackPointCompensation !== false, true);
                return true;
            }, false);
            if (!conv) {
                doc.close(SaveOptions.DONOTSAVECHANGES);
                doc = null;
                return jsObj(res.concat([
                    "ok", false,
                    "error", 'профиль "' + job.profile + '" не установлен в системе '
                           + "или конверсия не удалась — положите .icc в "
                           + "/Library/ColorSync/Profiles/"]));
            }
        }

        var opts = new TiffSaveOptions();
        safe(function () { opts.imageCompression = TIFFEncoding.TIFFLZW; return true; }, false);
        safe(function () { opts.embedColorProfile = true; return true; }, false);
        safe(function () { opts.layers = false; return true; }, false);
        safe(function () { opts.alphaChannels = true; return true; }, false);
        safe(function () { opts.spotColors = false; return true; }, false);
        doc.saveAs(new File(String(job.dst)), opts, true, Extension.LOWERCASE);

        var w = safe(function () { return doc.width.as("px"); }, null);
        var h = safe(function () { return doc.height.as("px"); }, null);

        doc.close(SaveOptions.DONOTSAVECHANGES);
        doc = null;

        return jsObj(res.concat([
            "ok", true,
            "merged", merged.join("|"),
            "mergedCount", merged.length,
            "modeBefore", wasMode,
            "widthPx", w,
            "heightPx", h,
            "warnings", warnings.join("|"),
            "error", ""
        ]));
    } catch (e) {
        if (doc !== null) {
            safe(function () { doc.close(SaveOptions.DONOTSAVECHANGES); return true; }, false);
        }
        return jsObj(res.concat([
            "ok", false,
            "warnings", warnings.join("|"),
            "error", String(e) + (e.line ? (" (line " + e.line + ")") : "")]));
    }
}

function main() {
    var p = readParams();
    if (!p || !p.outPath) {
        throw new Error("Не найден файл параметров: " + PARAMS_PATH);
    }
    var jobs = p.jobs || [];
    var outPath = String(p.outPath);
    var nonce = String(p.nonce || "");

    // Без этого Photoshop может встать на диалоге («профиль не совпадает»,
    // «нет шрифтов») и подвесить весь пайплайн.
    var prevDialogs = app.displayDialogs;
    app.displayDialogs = DialogModes.NO;

    var results = [];
    try {
        for (var i = 0; i < jobs.length; i++) {
            results.push(processJob(jobs[i]));
        }
    } finally {
        app.displayDialogs = prevDialogs;
    }

    var head = [
        '"schema":1',
        '"nonce":' + jsVal(nonce),
        '"appVersion":' + jsVal(safe(function () { return app.version; }, "")),
        '"jobCount":' + jsVal(jobs.length),
        '"results":[' + results.join(",") + "]"
    ];

    var out = new File(outPath);
    out.encoding = "UTF-8";
    out.lineFeed = "Unix";
    out.open("w");
    out.write("{" + head.join(",") + "}");
    out.close();

    return "OK " + jobs.length;
}

try {
    main();
} catch (err) {
    var ep = safe(function () { return readParams().outPath; },
                  (__args.length > 0 ? String(__args[0]) : ""));
    var ef = new File(String(ep) + ".error");
    ef.encoding = "UTF-8";
    ef.open("w");
    ef.write(String(err) + (err.line ? (" (line " + err.line + ")") : ""));
    ef.close();
    throw err;
}
