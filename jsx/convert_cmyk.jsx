/*
 * convert_cmyk.jsx  (Adobe Photoshop)
 * ----------------------------------
 * Делит цвет в CMYK средствами Photoshop. Включается настройкой
 * color_engine = "photoshop"; при значении "magick" (по умолчанию) этот
 * скрипт не вызывается вовсе, и цветоделение делает командная строка.
 *
 * Делается ТОЛЬКО конверсия цвета. Результат пишется во ВРЕМЕННЫЙ TIFF,
 * дальше его подхватывает prepress.py и гонит через обычную обработку —
 * ресемплинг, шарпинг, запись итогового TIFF. Исходник не меняется.
 * Разделение намеренное: так включение движка не трогает ни резкость,
 * ни геометрию, и отличаться в оттиске может только сама конверсия.
 *
 * Порядок внутри одного файла:
 *   1. режим, который нельзя конвертировать напрямую (Indexed, Bitmap),
 *      переводится в обычный;
 *   2. если в файле нет своего ICC — назначается исходный профиль
 *      (srcProfile), тот же самый, что подставил бы magick ключом -profile;
 *   3. слои сводятся — magick всё равно берёт композит, а так он предсказуем;
 *   4. convertProfile в целевой профиль с заданным intent и BPC;
 *   5. запись TIFF LZW с встроенным профилем.
 *
 * Вызывается из prepress.py через AppleScript:
 *   tell application id "com.adobe.Photoshop"
 *       do javascript file ".../convert_cmyk.jsx"
 *   end tell
 *
 * Параметры и результат — файлами (см. комментарий в collect_links.jsx):
 *   ~/Library/Application Support/prepress_automation/ps_params.json
 *   {"jobs":[{"src":..,"dst":..,"profile":"ISO coated ...",
 *             "srcProfile":"sRGB IEC61966-2.1" | "",
 *             "intent":"RELATIVECOLORIMETRIC","blackPointCompensation":true}],
 *    "outPath":"...", "nonce":"..."}
 *
 * profile и srcProfile — НАЗВАНИЯ профилей внутри .icc (Photoshop адресует
 * профили именно так), их считает prepress.py через icc_description().
 *
 * Массив jobs поддержан не про запас: цветоделение вызывается по файлу за раз,
 * потому что должно идти после ИИ-апскейла, а тот работает внутри цикла.
 * Пакетный вызов, если понадобится, не потребует правок здесь.
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

        // Indexed и Bitmap конвертировать профилем нельзя — сначала в обычный
        // режим. Для Indexed это RGB (палитра разворачивается без потерь),
        // для Bitmap — Grayscale, иначе changeMode откажет.
        if (safe(function () { return doc.mode === DocumentMode.INDEXEDCOLOR; }, false)) {
            doc.changeMode(ChangeMode.RGB);
            warnings.push("Indexed mode expanded to RGB before separation");
        } else if (safe(function () { return doc.mode === DocumentMode.BITMAP; }, false)) {
            doc.changeMode(ChangeMode.GRAYSCALE);
            warnings.push("Bitmap mode converted to Grayscale before separation");
        }

        // Исходный профиль назначаем, только если своего в файле нет —
        // ровно та же развилка, что у magick с ключом -profile.
        if (job.srcProfile) {
            // Назначение профиля в Photoshop — это присваивание
            // colorProfileName, а не метод: assignProfile в объектной модели
            // нет вовсе (ReferenceError). Тип при этом сам становится CUSTOM.
            var assignErr = null;
            try {
                doc.colorProfileName = String(job.srcProfile);
            } catch (ae) {
                assignErr = String(ae);
            }
            if (assignErr !== null) {
                doc.close(SaveOptions.DONOTSAVECHANGES);
                doc = null;
                return jsObj(res.concat([
                    "ok", false,
                    "error", 'не удалось назначить исходный профиль "'
                           + job.srcProfile + '": ' + assignErr
                           + " — Photoshop адресует профили названием, оно должно "
                           + "совпадать с тем, что показывает «Назначить профиль»"]));
            }
        }

        // Слои сводим сами: иначе saveAs с layers=false свёл бы их молча,
        // а нам нужен предсказуемый композит — тот же, что взял бы magick.
        if (safe(function () { return doc.layers.length; }, 1) > 1) {
            safe(function () { doc.flatten(); return true; }, false);
        }

        var convErr = null;
        try {
            doc.convertProfile(String(job.profile), intentByName(job.intent),
                               job.blackPointCompensation !== false, true);
        } catch (ce) {
            convErr = String(ce);
        }
        if (convErr !== null) {
            doc.close(SaveOptions.DONOTSAVECHANGES);
            doc = null;
            return jsObj(res.concat([
                "ok", false,
                "error", 'не удалось перевести в профиль "' + job.profile + '": '
                       + convErr + " — если профиля нет в системе, положите .icc "
                       + "в /Library/ColorSync/Profiles/"]));
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
        var nowMode = safe(function () { return String(doc.mode); }, "");

        doc.close(SaveOptions.DONOTSAVECHANGES);
        doc = null;

        return jsObj(res.concat([
            "ok", true,
            "modeBefore", wasMode,
            "modeAfter", nowMode,
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
