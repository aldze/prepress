/*
 * outline_text.jsx  (Adobe Illustrator)
 * -------------------------------------
 * Переводит в кривые живой текст и раскладывает плашечные краски (Pantone
 * и прочие) в CMYK в векторных файлах, связанных с документом InDesign.
 * Каждый файл открывается, правится и сохраняется ПОД НОВЫМ ИМЕНЕМ —
 * оригинал не трогается (его переносит в originals уже prepress.py).
 *
 * Вызывается из prepress.py через AppleScript:
 *   tell application id "com.adobe.illustrator"
 *       do javascript (POSIX file ".../outline_text.jsx")
 *   end tell
 *
 * Параметры и результат — файлами (см. комментарий в collect_links.jsx):
 *   ~/Library/Application Support/prepress_automation/ai_params.json
 *   {"jobs":[{"src":..,"dst":..,"format":"ai|eps|pdf",
 *             "outlineText":true,"convertSpots":true,
 *             "expectedWidthPt":..,"expectedHeightPt":..}],
 *    "outPath":"...", "nonce":"..."}
 *
 * ВАЖНО про цвет: раскладку плашки в CMYK Illustrator делает своим рабочим
 * пространством CMYK (Color Settings). Скрипт этим пространством управлять
 * не может — он только сообщает, в каком режиме был документ. Рабочий CMYK
 * в Illustrator должен совпадать с целевым профилем препресса.
 */

var __args = arguments;

var PARAMS_PATH = Folder.userData.fsName + "/prepress_automation/ai_params.json";

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

// --- слои: снять блокировку и показать скрытое ----------------------------
//
// createOutline() не работает на заблокированном и скрытом тексте. Состояние
// слоёв запоминаем и возвращаем как было — файл сохраняется под новым именем,
// но выглядеть он должен точно так же.

function walkLayers(layers, out) {
    for (var i = 0; i < layers.length; i++) {
        var L = layers[i];
        out.push(L);
        safe(function () { walkLayers(L.layers, out); return true; }, false);
    }
    return out;
}

function unlockLayers(doc) {
    var state = [];
    var all = safe(function () { return walkLayers(doc.layers, []); }, []);
    for (var i = 0; i < all.length; i++) {
        var L = all[i];
        state.push({
            layer: L,
            locked: safe(function () { return L.locked; }, false),
            visible: safe(function () { return L.visible; }, true)
        });
        safe(function () { L.locked = false; return true; }, false);
        safe(function () { L.visible = true; return true; }, false);
    }
    return state;
}

function restoreLayers(state) {
    for (var i = 0; i < state.length; i++) {
        var st = state[i];
        safe(function () { st.layer.locked = st.locked; return true; }, false);
        safe(function () { st.layer.visible = st.visible; return true; }, false);
    }
}

// --- шрифты ---------------------------------------------------------------
//
// Если шрифта нет в системе, Illustrator подставит другой — и в кривые уйдёт
// НЕ ТО, что было в файле. Такой файл лучше не трогать вовсе.

function missingFonts(doc) {
    var missing = [], seen = {};
    var frames = safe(function () { return doc.textFrames; }, null);
    if (!frames) { return missing; }
    var limit = Math.min(frames.length, 500);
    for (var i = 0; i < limit; i++) {
        var tf = frames[i];
        var names = [];
        var n1 = safe(function () {
            return tf.textRange.characterAttributes.textFont.name;
        }, null);
        if (n1) { names.push(n1); }
        // Во фрейме может быть несколько шрифтов — смотрим ещё и по абзацам.
        var paras = safe(function () { return tf.paragraphs; }, null);
        var plimit = paras ? Math.min(paras.length, 50) : 0;
        for (var k = 0; k < plimit; k++) {
            var pn = safe(function () {
                return paras[k].characterAttributes.textFont.name;
            }, null);
            if (pn) { names.push(pn); }
        }
        for (var j = 0; j < names.length; j++) {
            var name = names[j];
            if (seen[name]) { continue; }
            seen[name] = true;
            var installed = safe(function () {
                app.textFonts.getByName(name);
                return true;
            }, false);
            if (!installed) { missing.push(name); }
        }
    }
    return missing;
}

// --- текст в кривые -------------------------------------------------------

function outlineText(doc, warnings) {
    var frames = [];
    var live = safe(function () { return doc.textFrames; }, null);
    if (!live) { return 0; }
    for (var i = 0; i < live.length; i++) { frames.push(live[i]); }

    var done = 0;
    // Идём с конца: createOutline() удаляет исходный текстовый объект.
    for (var k = frames.length - 1; k >= 0; k--) {
        var tf = frames[k];
        var wasHidden = safe(function () { return tf.hidden; }, false);
        safe(function () { tf.locked = false; return true; }, false);
        safe(function () { tf.hidden = false; return true; }, false);
        var grp = null;
        try {
            grp = tf.createOutline();
            done++;
        } catch (e) {
            warnings.push("text object not outlined: " + e);
            continue;
        }
        if (wasHidden && grp) {
            safe(function () { grp.hidden = true; return true; }, false);
        }
    }
    return done;
}

// --- плашки в процессные краски -------------------------------------------

function convertSpots(doc, warnings) {
    var names = [];
    var spots = safe(function () { return doc.spots; }, null);
    if (!spots) { return names; }

    var isCmyk = safe(function () {
        return doc.documentColorSpace === DocumentColorSpace.CMYK;
    }, true);

    for (var i = 0; i < spots.length; i++) {
        var sp = spots[i];
        var nm = safe(function () { return sp.name; }, "");
        if (nm === "[Registration]" || nm === "[None]") { continue; }
        var ct = safe(function () { return sp.colorType; }, null);
        if (ct !== null && ct !== ColorModel.SPOT) { continue; }

        // Плашки из библиотек Pantone заданы в Lab. Пересчитываем в CMYK явно,
        // чтобы в файле остались числа красок, а не Lab-значение.
        var col = safe(function () { return sp.color; }, null);
        var tn = col ? safe(function () { return col.typename; }, "") : "";
        if (isCmyk && tn === "LabColor") {
            var conv = safe(function () {
                return app.convertSampleColor(
                    ImageColorSpace.LAB,
                    [col.l, col.a, col.b],
                    ImageColorSpace.CMYK,
                    ColorConvertPurpose.defaultpurpose);
            }, null);
            if (conv && conv.length === 4) {
                safe(function () {
                    var c = new CMYKColor();
                    c.cyan = conv[0]; c.magenta = conv[1];
                    c.yellow = conv[2]; c.black = conv[3];
                    sp.color = c;
                    return true;
                }, false);
            } else {
                warnings.push("spot " + nm + ": could not convert Lab to CMYK, "
                              + "разложение сделает Illustrator при сохранении");
            }
        }

        var ok = safe(function () { sp.colorType = ColorModel.PROCESS; return true; }, false);
        if (ok) { names.push(nm); }
        else { warnings.push("spot " + nm + ": could not convert to process colours"); }
    }
    return names;
}

// --- сохранение -----------------------------------------------------------

function saveDoc(doc, dst, fmt) {
    var f = new File(dst);
    if (fmt === "ai") {
        var oa = new IllustratorSaveOptions();
        safe(function () { oa.pdfCompatible = true; return true; }, false);
        safe(function () { oa.embedICCProfile = true; return true; }, false);
        doc.saveAs(f, oa);
    } else if (fmt === "pdf") {
        var op = new PDFSaveOptions();
        // Без «сохранения возможности редактирования» в PDF не попадает
        // приватная копия исходной вёрстки — иначе там остался бы живой текст.
        safe(function () { op.preserveEditability = false; return true; }, false);
        safe(function () { op.generateThumbnails = false; return true; }, false);
        safe(function () { op.colorConversionID = ColorConversion.None; return true; }, false);
        doc.saveAs(f, op);
    } else if (fmt === "eps") {
        var oe = new EPSSaveOptions();
        safe(function () { oe.includeDocumentThumbnails = false; return true; }, false);
        safe(function () { oe.embedAllFonts = true; return true; }, false);
        doc.saveAs(f, oe);
    } else {
        throw new Error("формат не поддерживается: " + fmt);
    }
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
        return jsObj(res.concat(["ok", false, "error", "Illustrator не открыл файл: " + e]));
    }

    var layerState = null;
    try {
        // Illustrator открывает PDF по одной из его рамок (Media/Crop/Trim/...).
        // Если он взял не ту, монтажная область не совпадёт с MediaBox — тогда
        // новый файл встал бы в макет со сдвигом. Такой файл не трогаем.
        if (job.expectedWidthPt) {
            var ab = safe(function () { return doc.artboards[0].artboardRect; }, null);
            if (ab) {
                var w = Math.abs(ab[2] - ab[0]);
                var h = Math.abs(ab[1] - ab[3]);
                if (Math.abs(w - job.expectedWidthPt) > 1.0 ||
                    Math.abs(h - job.expectedHeightPt) > 1.0) {
                    doc.close(SaveOptions.DONOTSAVECHANGES);
                    doc = null;
                    return jsObj(res.concat([
                        "ok", false,
                        "error", "Illustrator открыл PDF по другой рамке: монтажная "
                               + "область " + w.toFixed(1) + "x" + h.toFixed(1)
                               + " pt вместо " + job.expectedWidthPt + "x"
                               + job.expectedHeightPt + " pt — файл не тронут, "
                               + "переведите вручную"]));
                }
            }
            var nab = safe(function () { return doc.artboards.length; }, 1);
            if (nab > 1) {
                warnings.push("the PDF has several artboards (" + nab
                              + ") — проверьте, что в макет встала нужная");
            }
        }

        var outlined = 0;
        if (job.outlineText) {
            var miss = missingFonts(doc);
            if (miss.length) {
                doc.close(SaveOptions.DONOTSAVECHANGES);
                doc = null;
                return jsObj(res.concat([
                    "ok", false,
                    "error", "в системе нет шрифтов (" + miss.join(", ")
                           + ") — Illustrator подставил бы другие, файл не тронут"]));
            }
            layerState = unlockLayers(doc);
            outlined = outlineText(doc, warnings);
        }

        var spots = [];
        if (job.convertSpots) {
            var isRgb = safe(function () {
                return doc.documentColorSpace === DocumentColorSpace.RGB;
            }, false);
            if (isRgb) {
                var conv = safe(function () {
                    app.executeMenuCommand("doc-color-cmyk");
                    return true;
                }, false);
                warnings.push(conv
                    ? "документ был в RGB — переведён в CMYK рабочим пространством Illustrator"
                    : "документ в RGB, перевести в CMYK не удалось — плашки разложены в RGB");
            }
            spots = convertSpots(doc, warnings);
        }

        // Текст может прятаться там, куда createOutline() не достаёт.
        var left = safe(function () { return doc.textFrames.length; }, 0);
        if (job.outlineText && left > 0) {
            warnings.push("text objects left: " + left);
        }
        var syms = safe(function () { return doc.symbolItems.length; }, 0);
        if (job.outlineText && syms > 0) {
            warnings.push("the file has symbols (" + syms + ") — text inside "
                          + "a symbol definition is not outlined");
        }
        var placed = safe(function () { return doc.placedItems.length; }, 0);
        if (placed > 0) {
            warnings.push("the file has nested links (" + placed
                          + ") — их содержимое не проверялось");
        }
        var graphs = safe(function () { return doc.graphItems.length; }, 0);
        if (graphs > 0) {
            warnings.push("the file has graphs (" + graphs
                          + ") — их подписи в кривые не переводятся");
        }

        if (layerState) { restoreLayers(layerState); layerState = null; }
        saveDoc(doc, String(job.dst), String(job.format || "").toLowerCase());

        var space = safe(function () {
            return doc.documentColorSpace === DocumentColorSpace.CMYK ? "CMYK" : "RGB";
        }, "");

        doc.close(SaveOptions.DONOTSAVECHANGES);
        doc = null;

        return jsObj(res.concat([
            "ok", true,
            "outlined", outlined,
            "spotsConverted", spots.join(", "),
            "remainingText", left,
            "documentColorSpace", space,
            "warnings", warnings.join("|"),
            "error", ""
        ]));
    } catch (e) {
        if (layerState) { restoreLayers(layerState); }
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

    // Модальные окна («шрифты отсутствуют», «обновить текст») повесили бы
    // весь пайплайн: скрипт ждёт Illustrator, Illustrator ждёт человека.
    var prev = app.userInteractionLevel;
    app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;

    var results = [];
    try {
        for (var i = 0; i < jobs.length; i++) {
            results.push(processJob(jobs[i]));
        }
    } finally {
        app.userInteractionLevel = prev;
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
