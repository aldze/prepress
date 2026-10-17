/*
 * relink.jsx
 * ----------
 * Переподключает связи документа со старых файлов на новые (обработанные TIFF)
 * и сохраняет документ. Масштаб и положение картинки во фрейме сохраняются:
 * плотность (ppi) нового файла посчитана в prepress.py так, чтобы физический
 * размер изображения в макете не изменился. На всякий случай масштаб
 * запоминается до relink и восстанавливается после, если InDesign его сбил.
 *
 * arguments[0] — POSIX-путь к .indd; "-" или пусто = активный документ
 * arguments[1] — POSIX-путь к JSON-карте вида
 *                {"save":true,"saveAs":"/path/doc_CMYK.indd",
 *                 "docWasModified":false,
 *                 "map":[{"oldPath":"...","newPath":"..."}, ...]}
 *                saveAs (необязательный) — сохранить документ КАК новый файл;
 *                исходный .indd при этом остаётся на диске нетронутым.
 *                docWasModified — были ли несохранённые правки до прогона;
 *                нужен, когда map пуст (обрабатывать было нечего).
 * arguments[2] — POSIX-путь для JSON-отчёта
 */

var __args = arguments;

/* Параметры берём из файла, а не из arguments — прерванный прогон оставляет
   движок скриптов InDesign в состоянии, когда он отдаёт следующим вызовам
   аргументы предыдущего. См. комментарий в collect_links.jsx. */
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

function readFileText(p) {
    var f = new File(p);
    if (!f.exists) { throw new Error("Файл не найден: " + p); }
    f.encoding = "UTF-8";
    f.open("r");
    var t = f.read();
    f.close();
    return t;
}

// /tmp и /private/tmp — один и тот же каталог; InDesign отдаёт то один вариант,
// то другой, поэтому пути сравниваем в канонической форме.
function canonPath(p) {
    p = String(p || "");
    if (p.indexOf("/private/") === 0) { p = p.substring(8); }
    return p;
}

// doc.fullName бросает "Unsaved documents have no full name", если в документе
// есть несохранённые изменения. doc.filePath работает всегда.
function docFullPath(d) {
    var p = safe(function () { return d.fullName.fsName; }, "");
    if (!p) { p = safe(function () { return d.filePath.fsName + "/" + d.name; }, ""); }
    return p;
}

function resolveDocument(docPath) {
    if (!docPath || docPath === "-") {
        if (app.documents.length === 0) {
            throw new Error("Нет открытых документов, и путь к .indd не передан.");
        }
        return app.activeDocument;
    }
    var f = new File(docPath);
    if (!f.exists) { throw new Error("Файл не найден: " + docPath); }
    var want = canonPath(f.fsName);
    for (var i = 0; i < app.documents.length; i++) {
        if (canonPath(docFullPath(app.documents[i])) === want) { return app.documents[i]; }
    }
    return app.open(f);
}

// После relink() объект Link пересоздаётся и его id меняется, а вот id самой
// картинки (Image) остаётся прежним. Поэтому размещения адресуем по id графики.
function graphicIdsForPath(doc, oldPath) {
    var ids = [];
    var want = canonPath(oldPath);
    var links = doc.links;
    for (var i = 0; i < links.length; i++) {
        var lnk = links[i];
        var fp = safe(function () { return lnk.filePath; }, null);
        if (canonPath(fp) !== want) { continue; }
        var gid = safe(function () { return lnk.parent.id; }, null);
        if (gid !== null) { ids.push(gid); }
    }
    return ids;
}

function findLinkByGraphicId(doc, gid) {
    var links = doc.links;
    for (var i = 0; i < links.length; i++) {
        var lnk = links[i];
        var id = safe(function () { return lnk.parent.id; }, null);
        if (id === gid) { return lnk; }
    }
    return null;
}

/*
 * Габариты объекта в заданной системе координат: [left, top, right, bottom].
 * geometricBounds у повёрнутого объекта — габаритная рамка в координатах
 * линеек, по ней позиционировать нельзя. resolve() считает честно, а
 * PARENT_COORDINATES для картинки — это система координат её ФРЕЙМА.
 */
function boxIn(item, space) {
    var tl = item.resolve([AnchorPoint.TOP_LEFT_ANCHOR,
                           BoundingBoxLimits.GEOMETRIC_PATH_BOUNDS,
                           CoordinateSpaces.INNER_COORDINATES], space)[0];
    var br = item.resolve([AnchorPoint.BOTTOM_RIGHT_ANCHOR,
                           BoundingBoxLimits.GEOMETRIC_PATH_BOUNDS,
                           CoordinateSpaces.INNER_COORDINATES], space)[0];
    return [tl[0], tl[1], br[0], br[1]];
}

/*
 * Ставит картинку масштабом 100% и возвращает её на место.
 *
 * Плотность нового файла посчитана в prepress.py так, что при 100% физический
 * размер тот же, что был. Остаётся положение: после обрезки картинка стала
 * меньше, и её левый верхний угол должен оказаться ровно там, где кончился
 * отрезанный запас (padLeftPt / padTopPt — от левого верхнего угла фрейма).
 *
 * Сдвигаем матрицей в системе координат фрейма, а не присваиванием
 * geometricBounds: у повёрнутого фрейма bounds — габаритная рамка, и запись
 * в неё развернула бы картинку. transform с PARENT_COORDINATES работает
 * в осях фрейма, поэтому поворот фрейма ничего не ломает.
 *
 * Возвращает строку с описанием того, что сделано, — она уходит в отчёт.
 */
function placeAt(g, place) {
    var notes = [];
    var frame = safe(function () { return g.parent; }, null);
    if (frame === null) { return "нет фрейма"; }

    var want = Number(place.scale);
    if (want > 0) {
        var before = safe(function () { return g.absoluteHorizontalScale; }, null);
        safe(function () { g.absoluteHorizontalScale = want; return true; }, false);
        safe(function () { g.absoluteVerticalScale = want; return true; }, false);
        var after = safe(function () { return g.absoluteHorizontalScale; }, null);
        if (before !== null && after !== null) {
            notes.push("масштаб " + Math.round(before * 100) / 100 + "% -> "
                       + Math.round(after * 100) / 100 + "%");
        }
    }

    var cur = safe(function () { return boxIn(g, CoordinateSpaces.PARENT_COORDINATES); }, null);
    var frm = safe(function () { return boxIn(frame, CoordinateSpaces.INNER_COORDINATES); }, null);
    if (cur === null || frm === null) { return notes.join(", ") + "; положение не выправлено"; }

    var dx = (frm[0] + Number(place.padLeftPt)) - cur[0];
    var dy = (frm[1] + Number(place.padTopPt)) - cur[1];
    if (Math.abs(dx) > 0.01 || Math.abs(dy) > 0.01) {
        var moved = safe(function () {
            var mtx = app.transformationMatrices.add({
                horizontalTranslation: dx, verticalTranslation: dy });
            g.transform(CoordinateSpaces.PARENT_COORDINATES,
                        AnchorPoint.CENTER_ANCHOR, mtx);
            return true;
        }, false);
        notes.push(moved ? ("сдвиг " + Math.round(dx * 100) / 100 + ", "
                            + Math.round(dy * 100) / 100 + " пт")
                         : "сдвинуть не удалось");
    }
    return notes.join(", ");
}

function main() {
    var docPath = "", mapPath = "", outPath = "", nonce = "";

    var p = readParams();
    if (p && p.mapPath && p.outPath) {
        docPath = String(p.docPath || "");
        mapPath = String(p.mapPath);
        outPath = String(p.outPath);
        nonce = String(p.nonce || "");
    } else {
        docPath = (__args.length > 0) ? String(__args[0]) : "";
        mapPath = (__args.length > 1) ? String(__args[1]) : "";
        outPath = (__args.length > 2) ? String(__args[2]) : "";
    }
    if (!mapPath || !outPath) {
        throw new Error("Не переданы параметры: нет ни файла (" + PARAMS_PATH +
                        "), ни arguments [docPath, mapPath, reportPath].");
    }

    // В ExtendScript нет JSON.parse — читаем JSON как литерал через eval.
    var payload = eval("(" + readFileText(mapPath) + ")");
    var mapping = payload.map || [];
    var doSave = (payload.save !== false);
    var saveAs = payload.saveAs ? String(payload.saveAs) : "";
    /* Были ли в документе несохранённые правки ДО прогона. Замеряет
       collect_links.jsx, потому что сам он документ уже успел «испачкать»:
       InDesign помечает документ изменённым от любого присваивания настройке
       линеек, даже если значение то же. Здесь doc.modified всегда true и
       ни о чём не говорит. */
    var wasModified = (payload.docWasModified === true);

    var doc = resolveDocument(docPath);
    var results = [];
    var relinkedTotal = 0;

    // Отключаем диалоги, чтобы InDesign не завис на модальном окне.
    var prevInteraction = app.scriptPreferences.userInteractionLevel;
    app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;

    try {
        for (var m = 0; m < mapping.length; m++) {
            var oldPath = String(mapping[m].oldPath);
            var newPath = String(mapping[m].newPath);
            var place = mapping[m].place ? mapping[m].place : null;
            var newFile = new File(newPath);

            if (!newFile.exists) {
                results.push(jsObj([
                    "oldPath", oldPath, "newPath", newPath,
                    "relinked", 0, "ok", false,
                    "error", "Новый файл не найден на диске"
                ]));
                continue;
            }

            var count = 0;
            var errMsg = "";
            var scaleWarnings = [];

            // Одна и та же картинка может стоять в макете несколько раз.
            var gids = graphicIdsForPath(doc, oldPath);

            for (var k = 0; k < gids.length; k++) {
                var gid = gids[k];
                var lnk = findLinkByGraphicId(doc, gid);
                if (lnk === null) { continue; }

                var g = safe(function () { return lnk.parent; }, null);
                var sh = safe(function () { return g.absoluteHorizontalScale; }, null);
                var sv = safe(function () { return g.absoluteVerticalScale; }, null);
                // Держимся за ФРЕЙМ: relink пересоздаёт и связь, и саму графику,
                // а фрейм остаётся тем же объектом. Раньше графику искали только
                // по прежнему id, и когда id менялся, весь блок после
                // переподключения молча пропускался — вместе с восстановлением
                // масштаба. Заметно это стало только на обрезке, где масштаб
                // выставляется принудительно.
                var frameRef = safe(function () { return g.parent; }, null);

                try {
                    lnk.relink(newFile);
                } catch (e1) {
                    errMsg = String(e1);
                    continue;
                }

                var fresh = findLinkByGraphicId(doc, gid);
                var g2 = (fresh !== null) ? safe(function () { return fresh.parent; }, null) : null;
                if (g2 === null && frameRef !== null) {
                    g2 = safe(function () { return frameRef.graphics[0]; }, null);
                }
                if (g2 !== null) {
                    safe(function () { g2.itemLink.update(); return true; }, false);

                    var sh2 = safe(function () { return g2.absoluteHorizontalScale; }, null);
                    var sv2 = safe(function () { return g2.absoluteVerticalScale; }, null);

                    if (place !== null && g2 !== null) {
                        // Файл обрезан и/или нормирован: масштаб выставляем
                        // сами, а не восстанавливаем прежний.
                        var pnote = placeAt(g2, place);
                        if (pnote) { scaleWarnings.push("id " + gid + ": " + pnote); }
                    } else {
                        // Страховка: если InDesign сбил масштаб — вернуть исходный.
                        if (sh !== null && sh2 !== null && Math.abs(sh - sh2) > 0.01) {
                            safe(function () { g2.absoluteHorizontalScale = sh; return true; }, false);
                            scaleWarnings.push("id " + gid + ": H " + sh2 + "->" + sh);
                        }
                        if (sv !== null && sv2 !== null && Math.abs(sv - sv2) > 0.01) {
                            safe(function () { g2.absoluteVerticalScale = sv; return true; }, false);
                            scaleWarnings.push("id " + gid + ": V " + sv2 + "->" + sv);
                        }
                    }
                }
                count++;
                relinkedTotal++;
            }

            results.push(jsObj([
                "oldPath", oldPath,
                "newPath", newPath,
                "relinked", count,
                "ok", (count > 0 && errMsg === ""),
                "error", errMsg,
                "scaleRestored", scaleWarnings.join("; ")
            ]));
        }

        var saved = false;
        var saveError = "";
        var savedTo = "";

        /* Когда сохранять.
           relinkedTotal > 0 — обычный случай: связи переподключены.
           Переподключать было нечего (map пуст) — тоже сохраняем: все картинки
           уже соответствовали требованиям, но копия с суффиксом всё равно
           нужна, дальше по цепочке ждут именно её. Если же копию делать не
           надо (суффикс уже в имени, saveAs пуст), писать в тот же файл имеет
           смысл только при несохранённых правках (wasModified) — иначе прогон
           впустую переписал бы файл и сбил дату.
           А вот map не пуст, но не переподключилось ничего — это провал всех
           relink, и сохранять нельзя: ошибка уехала бы в файл незамеченной. */
        var nothingToRelink = (mapping.length === 0);
        var saveNow = doSave && (relinkedTotal > 0 || (nothingToRelink &&
                      (saveAs !== "" || wasModified)));
        if (saveNow) {
            if (saveAs) {
                /* Сохранение под новым именем. Исходный .indd остаётся на диске
                   ровно таким, каким был до прогона: связи в нём указывают на
                   исходные картинки, и из него по-прежнему можно собрать PDF
                   для экранов. Дальше документ в InDesign — это уже новый файл. */
                try {
                    doc.save(new File(saveAs));
                    saved = true;
                    savedTo = saveAs;
                } catch (e1) {
                    saveError = "не удалось сохранить как " + saveAs + ": " + e1;
                }
            } else {
                try {
                    doc.save();
                    saved = true;
                    savedTo = docFullPath(doc);
                } catch (e2) {
                    // Если документ InDesign считает «несохранённым», doc.save()
                    // без аргумента попытается открыть диалог, а при NEVER_INTERACT
                    // бросит исключение. Тогда сохраняем по явному пути.
                    var target = docPath && docPath !== "-" ? docPath : docFullPath(doc);
                    if (target) {
                        try {
                            doc.save(new File(target));
                            saved = true;
                            savedTo = target;
                        } catch (e3) { saveError = String(e2) + " / " + String(e3); }
                    } else {
                        saveError = String(e2);
                    }
                }
            }
        }

        var head = [
            '"schema":1',
            '"nonce":' + jsVal(nonce),
            '"documentPath":' + jsVal(safe(function () { return doc.fullName.fsName; }, docPath)),
            '"relinkedTotal":' + jsVal(relinkedTotal),
            '"saved":' + jsVal(saved),
            '"savedTo":' + jsVal(savedTo),
            '"saveError":' + jsVal(saveError),
            '"results":[' + results.join(",") + "]"
        ];

        var out = new File(outPath);
        out.encoding = "UTF-8";
        out.lineFeed = "Unix";
        out.open("w");
        out.write("{" + head.join(",") + "}");
        out.close();
    } finally {
        app.scriptPreferences.userInteractionLevel = prevInteraction;
    }

    return "OK";
}

try {
    main();
} catch (err) {
    var ep = safe(function () { return readParams().outPath; },
                  (__args.length > 2 ? String(__args[2]) : ""));
    var ef = new File(String(ep) + ".error");
    ef.encoding = "UTF-8";
    ef.open("w");
    ef.write(String(err) + (err.line ? (" (line " + err.line + ")") : ""));
    ef.close();
    throw err;
}
