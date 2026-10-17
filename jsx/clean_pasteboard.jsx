/*
 * clean_pasteboard.jsx
 * --------------------
 * Убирает из документа картинки, лежащие НА ПОЛЯХ — за пределами страниц.
 *
 * Зачем. Такие кадры в вёрстке остаются от работы дизайнера: отложенный
 * вариант, обрезок, дубль другого масштаба. Для пайплайна они хуже мусора:
 *   - связь на файл, которого уже нет, даёт «missing» на каждом прогоне;
 *   - тот же файл, положенный на поля в другом масштабе, превращается
 *     во второе размещение — и обрезка по фрейму становится невозможной
 *     («размещён в макете 2 раза»), а ресемплинг идёт по слабейшему из них.
 *
 * Документ НЕ сохраняется: правка живёт в открытом документе и уезжает
 * в _CMYK.indd, когда его сохранит relink.jsx. Исходный файл на диске
 * остаётся нетронутым — это общее правило пайплайна.
 *
 * Вызывается из prepress.py через AppleScript:
 *   do script (POSIX file ".../clean_pasteboard.jsx") language javascript
 *       with arguments {"/path/doc.indd", "/path/out.json"}
 */

var __args = arguments;

var PARAMS_PATH = Folder.userData.fsName + "/prepress_automation/jsx_params.json";

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
        } else { out += c; }
    }
    return out;
}

function jsStr(v) { return '"' + jsEsc(v) + '"'; }

function safe(fn, fallback) {
    try { return fn(); } catch (e) { return fallback; }
}

function canonPath(p) {
    p = String(p || "");
    try { p = decodeURI(p); } catch (e) {}
    return p.replace(/\/+$/, "");
}

function docFullPath(d) {
    var p = safe(function () { return d.fullName.fsName; }, "");
    if (!p) { p = safe(function () { return d.filePath.fsName + "/" + d.name; }, ""); }
    return p;
}

function resolveDocument(docPath) {
    if (!docPath || docPath === "-") {
        if (app.documents.length === 0) {
            throw new Error("No open documents and no .indd path was given.");
        }
        return app.activeDocument;
    }
    var f = new File(docPath);
    if (!f.exists) { throw new Error("File not found: " + docPath); }
    var want = canonPath(f.fsName);
    for (var i = 0; i < app.documents.length; i++) {
        var d = app.documents[i];
        if (canonPath(docFullPath(d)) === want) { return d; }
    }
    return app.open(f);
}

/* Имя связи внутри объекта — для отчёта. Пустая строка, если графики нет. */
function graphicNames(item) {
    var names = [];
    var gs = safe(function () { return item.allGraphics; }, []);
    for (var i = 0; i < gs.length; i++) {
        var n = safe(function () { return gs[i].itemLink.name; }, "");
        names.push(n || "(embedded)");
    }
    return names;
}

/*
 * На полях ли объект.
 *
 * parentPage у объекта, целиком лежащего за пределами страниц разворота,
 * равен null — это и есть определение «на полях» в объектной модели
 * InDesign, а не наш подсчёт координат. Объект, свисающий со страницы
 * (в вылет или за обрез), parentPage имеет, и его мы НЕ трогаем: это
 * законная вёрстка, а не забытый кадр.
 */
function onPasteboard(item) {
    var page = safe(function () { return item.parentPage; }, undefined);
    return page === null;
}

function main() {
    var docPath = "", outPath = "", nonce = "", apply = true;
    var p = readParams();
    if (p && p.outPath) {
        docPath = String(p.docPath || "");
        outPath = String(p.outPath);
        nonce = String(p.nonce || "");
        /* Холостой прогон (--dry-run) обязан отвечать на вопрос «что
           уберётся», ничего не убирая: список считается тем же кодом,
           иначе отчёт и прогон разойдутся. */
        apply = (p.apply === undefined) ? true : !!p.apply;
    } else {
        docPath = (__args.length > 0) ? String(__args[0]) : "";
        outPath = (__args.length > 1) ? String(__args[1]) : "";
    }
    if (!outPath) {
        throw new Error("No output path: neither the params file ("
                        + PARAMS_PATH + ") nor arguments[1].");
    }

    var prevInteraction = app.scriptPreferences.userInteractionLevel;
    app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;

    var removed = [], kept = 0, failed = [];
    var doc;
    try {
        doc = resolveDocument(docPath);

        /* Только развороты самого документа. Мастер-страницы не трогаем:
           объект на полях мастера повторяется на всех страницах, которые
           им пользуются, и удалять его — правка вёрстки, а не уборка. */
        var spreads = doc.spreads;
        for (var s = 0; s < spreads.length; s++) {
            var items = spreads[s].pageItems;
            /* Идём С КОНЦА: удаление сдвигает коллекцию, и цикл вперёд
               пропускает каждый второй объект. */
            for (var i = items.length - 1; i >= 0; i--) {
                var item = items[i];
                var names = graphicNames(item);
                if (names.length === 0) { continue; }   // не картинка — не наше дело
                if (!onPasteboard(item)) { kept += names.length; continue; }
                var label = names.join(", ");
                if (!apply) { removed.push(label); continue; }
                try {
                    /* Запертый объект удалить нельзя, и молчать об этом
                       тоже нельзя: человек ждёт, что поля будут чистыми. */
                    if (safe(function () { return item.locked; }, false)) {
                        item.locked = false;
                    }
                    item.remove();
                    removed.push(label);
                } catch (e) {
                    failed.push(label + ": " + e);
                }
            }
        }
    } finally {
        app.scriptPreferences.userInteractionLevel = prevInteraction;
    }

    var parts = [];
    for (var r = 0; r < removed.length; r++) { parts.push(jsStr(removed[r])); }
    var fparts = [];
    for (var f2 = 0; f2 < failed.length; f2++) { fparts.push(jsStr(failed[f2])); }

    var out = new File(outPath);
    out.encoding = "UTF-8";
    out.lineFeed = "Unix";
    out.open("w");
    out.write("{" + [
        '"nonce":' + jsStr(nonce),
        '"removed":[' + parts.join(",") + "]",
        '"failed":[' + fparts.join(",") + "]",
        '"keptOnPages":' + kept,
        '"applied":' + (apply ? "true" : "false")
    ].join(",") + "}");
    out.close();

    return "OK " + removed.length;
}

try {
    main();
} catch (err) {
    var ep = safe(function () { return readParams().outPath; },
                  (__args.length > 1 ? String(__args[1]) : ""));
    var ef = new File(String(ep) + ".error");
    ef.encoding = "UTF-8";
    ef.open("w");
    ef.write(String(err) + (err.line ? (" (line " + err.line + ")") : ""));
    ef.close();
    throw err;
}
