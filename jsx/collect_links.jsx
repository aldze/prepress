/*
 * collect_links.jsx
 * -----------------
 * Читает все связи (links) документа InDesign и выгружает данные о них в JSON.
 * Никаких изменений в документ не вносит.
 *
 * Вызывается из prepress.py через AppleScript:
 *   do script (POSIX file ".../collect_links.jsx") language javascript
 *       with arguments {"/path/doc.indd", "/path/out.json"}
 *
 * arguments[0] — POSIX-путь к .indd; "-" или пусто = активный документ
 * arguments[1] — POSIX-путь, куда записать JSON
 */

var __args = arguments;

/* Параметры берём из файла, а не из arguments.
   Причина: если прогон прервать (Ctrl+C, зависшее модальное окно), движок
   скриптов InDesign застревает и начинает отдавать следующим вызовам аргументы
   предыдущего вызова — вплоть до перезапуска приложения. Файл от этого состояния
   не зависит. arguments остаются запасным вариантом для ручного запуска. */
var PARAMS_PATH = Folder.userData.fsName + "/prepress_automation/jsx_params.json";

function readParams() {
    var f = new File(PARAMS_PATH);
    if (!f.exists) { return null; }
    f.encoding = "UTF-8";
    f.open("r");
    var txt = f.read();
    f.close();
    if (!txt) { return null; }
    // В ExtendScript нет JSON.parse — читаем JSON-литерал через eval.
    return eval("(" + txt + ")");
}

// --- минимальный JSON-сериализатор (в ExtendScript нет объекта JSON) -------
// Всё, что вне ASCII, экранируется в \uXXXX — файл получается чистым ASCII,
// поэтому кириллица в именах файлов не зависит от кодировки при чтении в Python.
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

// Пары складываются в массив ["ключ", значение, "ключ", значение, ...],
// чтобы порядок полей в JSON был предсказуемым.
function jsObj(pairs) {
    var parts = [];
    for (var i = 0; i < pairs.length; i += 2) {
        parts.push('"' + jsEsc(pairs[i]) + '":' + jsVal(pairs[i + 1]));
    }
    return "{" + parts.join(",") + "}";
}

// --- безопасное чтение свойств DOM ----------------------------------------
function safe(fn, fallback) {
    try {
        var v = fn();
        if (v === undefined) { return fallback; }
        return v;
    } catch (e) {
        return fallback;
    }
}

function statusName(st) {
    if (st === LinkStatus.NORMAL) { return "NORMAL"; }
    if (st === LinkStatus.LINK_OUT_OF_DATE) { return "OUT_OF_DATE"; }
    if (st === LinkStatus.LINK_MISSING) { return "MISSING"; }
    if (st === LinkStatus.LINK_EMBEDDED) { return "EMBEDDED"; }
    if (st === LinkStatus.LINK_INACCESSIBLE) { return "INACCESSIBLE"; }
    return "OTHER";
}

// --- геометрия для проверки вылетов под обрез -----------------------------

function findSpread(item) {
    var p = item;
    for (var guard = 0; guard < 20 && p; guard++) {
        var cn = safe(function () { return p.constructor.name; }, "");
        if (cn === "Spread" || cn === "MasterSpread") { return p; }
        p = safe(function () { return p.parent; }, null);
    }
    return null;
}

/* Обрезной формат разворота = объединение габаритов его страниц.
   Внутренние края (корешок) в него не попадают — там разворот не режется. */
function spreadTrim(spread) {
    var pgs = safe(function () { return spread.pages; }, null);
    if (!pgs || pgs.length === 0) { return null; }
    var r = null;
    for (var i = 0; i < pgs.length; i++) {
        var b = safe(function () { return pgs[i].bounds; }, null);
        if (!b) { continue; }
        if (r === null) { r = [b[0], b[1], b[2], b[3]]; }
        else {
            r[0] = Math.min(r[0], b[0]); r[1] = Math.min(r[1], b[1]);
            r[2] = Math.max(r[2], b[2]); r[3] = Math.max(r[3], b[3]);
        }
    }
    return r;
}

function boundsPairs(prefix, b) {
    if (!b) {
        return [prefix + "Top", null, prefix + "Left", null,
                prefix + "Bottom", null, prefix + "Right", null];
    }
    return [prefix + "Top", b[0], prefix + "Left", b[1],
            prefix + "Bottom", b[2], prefix + "Right", b[3]];
}

// У связанного PDF/AI в макет вставлена конкретная страница и конкретная
// рамка обрезки — от них зависит, можно ли автоматически переводить файл
// в кривые (Illustrator открывает из PDF только одну страницу).
function pdfPageNumber(g) {
    return safe(function () { return g.pdfAttributes.pageNumber; }, null);
}

function pdfCropName(g) {
    var c = safe(function () { return g.pdfAttributes.pdfCrop; }, null);
    if (c === null) { return ""; }
    var names = ["CROP_ART", "CROP_BLEED", "CROP_CONTENT",
                 "CROP_CONTENT_ALL_LAYERS", "CROP_CONTENT_VISIBLE_LAYERS",
                 "CROP_MEDIA", "CROP_PDF", "CROP_TRIM"];
    for (var i = 0; i < names.length; i++) {
        var v = safe(function () { return PDFCrop[names[i]]; }, undefined);
        if (v !== undefined && v === c) { return names[i]; }
    }
    return String(c);
}

/*
 * Габариты объекта В ЗАДАННОЙ системе координат: [left, top, right, bottom].
 *
 * geometricBounds для повёрнутого объекта отдаёт габаритную рамку в координатах
 * линеек — по ней ни обрезать, ни позиционировать нельзя. resolve() же считает
 * честно: попросив у картинки PARENT_COORDINATES, получаем её положение
 * в системе координат ФРЕЙМА, а у фрейма INNER_COORDINATES — его собственный
 * прямоугольник в той же системе. Разница этих двух коробок и есть запас
 * картинки за фреймом с каждой стороны — та самая величина, на которую можно
 * обрезать. Работает и с повёрнутым фреймом: поворот у обоих объектов общий
 * и в разнице сокращается.
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

function boxPairs(prefix, box) {
    if (!box) {
        return [prefix + "Left", null, prefix + "Top", null,
                prefix + "Right", null, prefix + "Bottom", null];
    }
    return [prefix + "Left", box[0], prefix + "Top", box[1],
            prefix + "Right", box[2], prefix + "Bottom", box[3]];
}

function clippingName(g) {
    return safe(function () {
        var t = g.clippingPath.clippingType;
        if (t === ClippingPathType.NONE) { return "NONE"; }
        if (t === ClippingPathType.PHOTOSHOP_PATH) { return "PHOTOSHOP_PATH"; }
        if (t === ClippingPathType.ALPHA_CHANNEL) { return "ALPHA_CHANNEL"; }
        if (t === ClippingPathType.DETECT_EDGES) { return "DETECT_EDGES"; }
        if (t === ClippingPathType.USER_MODIFIED_PATH) { return "USER_MODIFIED_PATH"; }
        return "OTHER";
    }, "UNKNOWN");
}

// --- поиск / открытие документа -------------------------------------------

// /tmp и /private/tmp — один и тот же каталог; InDesign отдаёт то один вариант,
// то другой, поэтому пути сравниваем в канонической форме.
function canonPath(p) {
    p = String(p || "");
    if (p.indexOf("/private/") === 0) { p = p.substring(8); }
    return p;
}

// doc.fullName бросает "Unsaved documents have no full name", если в документе
// есть несохранённые изменения. doc.filePath работает всегда — собираем путь из
// него и имени документа.
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
    if (!f.exists) {
        throw new Error("Файл не найден: " + docPath);
    }
    var want = canonPath(f.fsName);
    for (var i = 0; i < app.documents.length; i++) {
        var d = app.documents[i];
        if (canonPath(docFullPath(d)) === want) { return d; }
    }
    return app.open(f);
}

function main() {
    var docPath = "", outPath = "", nonce = "";

    var p = readParams();
    if (p && p.outPath) {
        docPath = String(p.docPath || "");
        outPath = String(p.outPath);
        nonce = String(p.nonce || "");
    } else {
        docPath = (__args.length > 0) ? String(__args[0]) : "";
        outPath = (__args.length > 1) ? String(__args[1]) : "";
    }
    if (!outPath) {
        throw new Error("Не передан путь для выходного JSON: нет ни файла параметров ("
                        + PARAMS_PATH + "), ни arguments[1].");
    }

    // Документ с битыми связями или недостающими шрифтами открывается с
    // модальным окном, и весь пайплайн повисает намертво: скрипт ждёт InDesign,
    // InDesign ждёт человека. Гасим диалоги на время работы.
    var prevInteraction = app.scriptPreferences.userInteractionLevel;
    app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;
    var doc;
    try {
        doc = resolveDocument(docPath);
    } finally {
        app.scriptPreferences.userInteractionLevel = prevInteraction;
    }

    var docFull = docFullPath(doc);
    if (!docFull && docPath && docPath !== "-") {
        docFull = new File(docPath).fsName;
    }

    // Один и тот же файл может быть открыт в InDesign дважды — это ломает
    // логику: правки уйдут не в тот экземпляр. Предупреждаем явно.
    var sameOpen = 0;
    for (var q = 0; q < app.documents.length; q++) {
        if (canonPath(docFullPath(app.documents[q])) === canonPath(docFull)) { sameOpen++; }
    }

    /* Снимаем флаг «есть несохранённые правки» ДО того, как трогаем линейки:
       InDesign считает документ изменённым от любого присваивания настройке,
       даже если значение то же самое. Из-за этого замер после (см. ниже) всегда
       давал true — и предупреждение о несохранённых правках срабатывало на
       каждом прогоне впустую, а relink.jsx не мог по нему решить, надо ли
       вообще писать файл. */
    var modifiedBefore = safe(function () { return doc.modified; }, null);

    // Всю геометрию собираем в миллиметрах от левого верхнего угла разворота,
    // независимо от того, как настроены линейки у пользователя. В конце
    // настройки возвращаем как были.
    var vp = doc.viewPreferences;
    var oldUnitsH = safe(function () { return vp.horizontalMeasurementUnits; }, null);
    var oldUnitsV = safe(function () { return vp.verticalMeasurementUnits; }, null);
    var oldOrigin = safe(function () { return vp.rulerOrigin; }, null);
    var oldZero = safe(function () { return doc.zeroPoint; }, null);

    var records = [];
    var links = doc.links;
    var bleedPrefs = null;

    try {
    vp.horizontalMeasurementUnits = MeasurementUnits.MILLIMETERS;
    vp.verticalMeasurementUnits = MeasurementUnits.MILLIMETERS;
    vp.rulerOrigin = RulerOrigin.SPREAD_ORIGIN;
    doc.zeroPoint = [0, 0];

    var dp = doc.documentPreferences;
    bleedPrefs = [
        safe(function () { return dp.documentBleedTopOffset; }, null),
        safe(function () { return dp.documentBleedInsideOrLeftOffset; }, null),
        safe(function () { return dp.documentBleedBottomOffset; }, null),
        safe(function () { return dp.documentBleedOutsideOrRightOffset; }, null)
    ];

    for (var i = 0; i < links.length; i++) {
        var lnk = links[i];
        var g = safe(function () { return lnk.parent; }, null);

        var actualPpi = safe(function () { return g.actualPpi; }, null);
        var effPpi = safe(function () { return g.effectivePpi; }, null);

        // Габариты картинки, её фрейма и обрезного формата разворота —
        // для проверки вылетов под обрез.
        var frameObj = safe(function () { return g.parent; }, null);
        var imgB = safe(function () { return g.geometricBounds; }, null);
        var frmB = frameObj ? safe(function () { return frameObj.geometricBounds; }, null) : null;
        var spr = frameObj ? findSpread(frameObj) : null;
        var sprB = spr ? spreadTrim(spr) : null;
        var pgB = safe(function () { return g.parentPage.bounds; }, null);

        // У повёрнутых и наклонённых объектов geometricBounds — габаритная
        // рамка, а не сам объект; вылеты по ней считать нельзя.
        var rotG = safe(function () { return g.absoluteRotationAngle; }, 0) || 0;
        var shearG = safe(function () { return g.absoluteShearAngle; }, 0) || 0;
        var rotF = frameObj ? (safe(function () { return frameObj.absoluteRotationAngle; }, 0) || 0) : 0;
        var shearF = frameObj ? (safe(function () { return frameObj.absoluteShearAngle; }, 0) || 0) : 0;
        var reliable = (Math.abs(rotG) < 0.001 && Math.abs(shearG) < 0.001 &&
                        Math.abs(rotF) < 0.001 && Math.abs(shearF) < 0.001);

        var pairs = [
            "index", i,
            "id", safe(function () { return lnk.id; }, -1),
            "name", safe(function () { return lnk.name; }, ""),
            "filePath", safe(function () { return lnk.filePath; }, ""),
            "resourceURI", safe(function () { return lnk.linkResourceURI; }, ""),
            "status", statusName(safe(function () { return lnk.status; }, null)),
            "sizeBytes", safe(function () { return lnk.size; }, -1),
            "graphicClass", safe(function () { return g.constructor.name; }, "Unknown"),
            "imageTypeName", safe(function () { return g.imageTypeName; }, ""),
            "space", safe(function () { return String(g.space); }, ""),
            "profile", safe(function () { return String(g.profile); }, ""),
            "actualPpiH", actualPpi ? actualPpi[0] : null,
            "actualPpiV", actualPpi ? actualPpi[1] : null,
            "effectivePpiH", effPpi ? effPpi[0] : null,
            "effectivePpiV", effPpi ? effPpi[1] : null,
            "scaleH", safe(function () { return g.absoluteHorizontalScale; }, null),
            "scaleV", safe(function () { return g.absoluteVerticalScale; }, null),
            "rotation", safe(function () { return g.absoluteRotationAngle; }, null),
            "page", safe(function () { return String(g.parentPage.name); }, ""),
            "layer", safe(function () { return String(g.itemLayer.name); }, ""),
            "layerVisible", safe(function () { return g.itemLayer.visible; }, null),
            "clippingType", clippingName(g),
            "pdfPageNumber", pdfPageNumber(g),
            "pdfCrop", pdfCropName(g),
            "boundsReliable", reliable
        ];
        // Геометрия в системе координат фрейма — для обрезки по фрейму
        // и для установки масштаба ровно 100%. Отдельно от boundsReliable:
        // повёрнутый ФРЕЙМ обрезке не мешает, мешает только поворот самой
        // картинки ОТНОСИТЕЛЬНО фрейма (у неё своя, не абсолютная, величина).
        var innerImg = safe(function () {
            return boxIn(g, CoordinateSpaces.PARENT_COORDINATES); }, null);
        var innerFrm = frameObj ? safe(function () {
            return boxIn(frameObj, CoordinateSpaces.INNER_COORDINATES); }, null) : null;
        // Углы — АБСОЛЮТНЫЕ у обоих объектов. rotationAngle у картинки внутри
        // повёрнутого фрейма возвращает угол вместе с фреймом, а не свой
        // собственный (проверено: фрейм повёрнут на 22°, у картинки внутри
        // rotationAngle тоже 22). Поворот картинки ОТНОСИТЕЛЬНО фрейма —
        // это разница, её и считает prepress.py.
        pairs = pairs.concat([
            "absRotation", rotG,
            "absShear", shearG,
            "frameRotation", rotF,
            "frameShear", shearF
        ]);
        pairs = pairs.concat(boxPairs("innerImg", innerImg));
        pairs = pairs.concat(boxPairs("innerFrame", innerFrm));
        pairs = pairs.concat(boundsPairs("img", imgB));
        pairs = pairs.concat(boundsPairs("frame", frmB));
        pairs = pairs.concat(boundsPairs("spread", sprB));
        pairs = pairs.concat(boundsPairs("page", pgB));
        records.push(jsObj(pairs));
    }
    } finally {
        if (oldUnitsH !== null) { vp.horizontalMeasurementUnits = oldUnitsH; }
        if (oldUnitsV !== null) { vp.verticalMeasurementUnits = oldUnitsV; }
        if (oldOrigin !== null) { vp.rulerOrigin = oldOrigin; }
        if (oldZero !== null) { doc.zeroPoint = oldZero; }
    }

    var head = [
        '"schema":1',
        '"nonce":' + jsVal(nonce),
        '"documentName":' + jsVal(safe(function () { return doc.name; }, "")),
        '"documentPath":' + jsVal(docFull),
        '"documentModified":' + jsVal(modifiedBefore),
        '"documentSaved":' + jsVal(safe(function () { return doc.saved; }, null)),
        '"openCopies":' + jsVal(sameOpen),
        '"appVersion":' + jsVal(app.version),
        '"linkCount":' + jsVal(links.length),
        '"bleedTopMm":' + jsVal(bleedPrefs ? bleedPrefs[0] : null),
        '"bleedLeftMm":' + jsVal(bleedPrefs ? bleedPrefs[1] : null),
        '"bleedBottomMm":' + jsVal(bleedPrefs ? bleedPrefs[2] : null),
        '"bleedRightMm":' + jsVal(bleedPrefs ? bleedPrefs[3] : null),
        '"links":[' + records.join(",") + "]"
    ];

    var out = new File(outPath);
    out.encoding = "UTF-8";
    out.lineFeed = "Unix";
    out.open("w");
    out.write("{" + head.join(",") + "}");
    out.close();

    return "OK " + links.length;
}

try {
    main();
} catch (err) {
    // Ошибку тоже отдаём файлом — иначе Python её не увидит.
    var ep = safe(function () { return readParams().outPath; },
                  (__args.length > 1 ? String(__args[1]) : ""));
    var ef = new File(String(ep) + ".error");
    ef.encoding = "UTF-8";
    ef.open("w");
    ef.write(String(err) + (err.line ? (" (line " + err.line + ")") : ""));
    ef.close();
    throw err;
}
