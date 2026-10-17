/*
 * color_correct.jsx  (Adobe Photoshop)
 * ------------------------------------
 * Цветокоррекция памятных цветов: кожа, небо, зелень. Photoshop находит
 * области своим ИИ (Select Sky, Select People) и цветовым диапазоном,
 * замеряет средний цвет каждой области и — если печатные числа выходят
 * за норму — тянет их до ближайшей границы кривыми по каналам.
 *
 * Правка идёт в ИСХОДНОМ пространстве файла, ДО цветоделения: RGB-файл
 * правится по RGB, CMYK-файл — по CMYK. Решение при этом принимается по
 * ПЕЧАТНЫМ числам: средний цвет области переводится в целевой профиль
 * (для RGB-файла — документом 1x1 через настоящий ICC, см. inkOfRgb), и
 * норма проверяется уже там. Так на картинку не ложится вторая конверсия,
 * а правило остаётся тем же, каким его читает препресс-технолог по Info.
 *
 * Результат пишется во ВРЕМЕННЫЙ TIFF в том же пространстве, что и
 * исходник; дальше его подхватывает prepress.py и гонит через обычную
 * обработку — цветоделение, ресемплинг, шарпинг, запись. Исходник
 * не меняется. Если правит нечего, файл не пишется вовсе (changed=false),
 * и дальше по цепочке идёт исходный файл — лишней перезаписи не будет.
 *
 * Вызывается из prepress.py через AppleScript:
 *   tell application id "com.adobe.Photoshop"
 *       do javascript file ".../color_correct.jsx"
 *   end tell
 *
 * Параметры и результат — файлами (см. комментарий в collect_links.jsx):
 *   ~/Library/Application Support/prepress_automation/ps_params.json
 *   {"jobs":[{"src":..,"dst":..,"apply":true,"detectPx":2000,
 *             "maxShift":12,"targetProfile":"ISOcoated_v2...",
 *             "rgbProfile":"sRGB IEC61966-2.1","srcProfile":"",
 *             "intent":"RELATIVECOLORIMETRIC","blackPointCompensation":true,
 *             "colors":[{"name":"skin","detector":"skin","minArea":0.5,
 *                        "fuzziness":40,"detectFaces":true,
 *                        "gates":[...],"rules":[...]}, ...]}],
 *    "outPath":"...", "nonce":"..."}
 *
 * Правило и «ворота» — один и тот же объект:
 *   {"ch":"C","ref":"M","kind":"ratio","min":0.15,"max":0.45}
 *   kind = ratio (ch/ref), diff (ch-ref), abs (само значение ch).
 * Ворота только отсеивают область («не похоже на небо по числам»),
 * правило — тянет канал ch до ближайшей нарушенной границы. Числа живут
 * в config.json, здесь только арифметика: подбирать пороги приходится
 * по оттискам, и лазить за ними в .jsx было бы неудобно.
 *
 * Что здесь важно и проверено исполнением (osacompile такого не ловит):
 *
 * 1. colorRange с пресетом («Тона кожи», зелень по Lab) в CMYK-документе
 *    РОНЯЕТ Photoshop — не ошибка, а падение всего приложения. Поэтому
 *    детект всегда идёт по RGB-двойнику, а найденное переезжает в рабочий
 *    документ альфа-каналом.
 * 2. selectSky при отсутствии неба не отдаёт пустое выделение, а бросает
 *    ошибку «The parameters for command Select Sky are not currently
 *    valid». Это нормальный ответ «неба нет», а не сбой.
 * 3. Пересечение выделений — SelectionType.INTERSECT. INTERSECTION,
 *    которое просится по аналогии с REPLACE, даёт «Invalid enumeration
 *    value» уже в рантайме.
 * 4. doc.duplicate() НЕ переносит выделение — только каналы. Отсюда
 *    маски и переезжают каналами, а не выделением.
 * 5. После channels.add() активным становится сам альфа-канал, и заливка
 *    или кривая уходят в него, а не в картинку. Каждый раз, закончив
 *    с каналами, возвращаем doc.activeChannels = doc.componentChannels.
 * 6. Гистограмма канала считается ПО ВЫДЕЛЕНИЮ — на этом и держится
 *    замер среднего цвета области.
 * 7. В CMYK-документе значение канала инвертировано: краска в процентах
 *    равна (255 - значение) / 2.55. Сверено с colorSampler.
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

function errText(e) {
    return String(e).replace(/[\r\n]+/g, " ") + (e && e.line ? (" (line " + e.line + ")") : "");
}

function round1(v) { return Math.round(v * 10) / 10; }

/*
 * Замер времени по шагам. Не роскошь: на боевом прогоне этап памятных цветов
 * съел 90% времени, и без разбивки было не понять, где именно — в детекте,
 * в правке или в записи. Уходит в отчёт строкой и дальше в лог.
 */
var TIMER = { marks: [], last: 0 };

function tstart() { TIMER.marks = []; TIMER.last = (new Date()).getTime(); }

function tmark(label) {
    var now = (new Date()).getTime();
    TIMER.marks.push(label + ":" + (now - TIMER.last));
    TIMER.last = now;
}

function tdump() { return TIMER.marks.join("|"); }

function intentByName(name) {
    name = String(name || "").toUpperCase();
    if (name === "PERCEPTUAL") { return Intent.PERCEPTUAL; }
    if (name === "SATURATION") { return Intent.SATURATION; }
    if (name === "ABSOLUTECOLORIMETRIC") { return Intent.ABSOLUTECOLORIMETRIC; }
    return Intent.RELATIVECOLORIMETRIC;
}

// --- замеры ---------------------------------------------------------------

function componentCount(doc) {
    return safe(function () { return doc.componentChannels.length; }, 3);
}

/*
 * Среднее по каждому каналу внутри текущего выделения (0..255) и число
 * посчитанных пикселей. Гистограмма Photoshop честно ограничивается
 * выделением — проверено сверкой с заведомо известной областью.
 */
function meanChannels(doc) {
    var n = componentCount(doc);
    var means = [], count = 0;
    for (var i = 0; i < n; i++) {
        var h = doc.channels[i].histogram, sum = 0, c = 0;
        for (var k = 0; k < 256; k++) { sum += k * h[k]; c += h[k]; }
        if (!c) { return null; }
        count = c;
        means.push(sum / c);
    }
    return { means: means, count: count };
}

/* Доля выделенного в процентах кадра — по гистограмме первого канала. */
function selectionShare(doc) {
    var h = doc.channels[0].histogram, c = 0;
    for (var k = 0; k < 256; k++) { c += h[k]; }
    return c / (doc.width.as("px") * doc.height.as("px")) * 100;
}

function hasSelection(doc) {
    try { doc.selection.bounds; return true; } catch (e) { return false; }
}

function inkFromChannel(v) { return (255 - v) / 2.55; }
function channelFromInk(p) { return 255 - p * 2.55; }

/*
 * Перевод конкретного цвета через настоящие ICC-профили — силами самого
 * Photoshop, на документе 1x1. Своей ICC-математики в ExtendScript нет,
 * а спрашивать Python значило бы разорвать вызов надвое и оставить
 * документ открытым между заходами.
 */
function inkOfRgb(rgb, srcProfile, dstProfile, intent, bpc) {
    var d = app.documents.add(1, 1, 72, "prepress_probe", NewDocumentMode.RGB,
                              DocumentFill.WHITE);
    try {
        if (srcProfile) { safe(function () { d.colorProfileName = srcProfile; return true; }, false); }
        var col = new SolidColor();
        col.rgb.red = Math.max(0, Math.min(255, Math.round(rgb[0])));
        col.rgb.green = Math.max(0, Math.min(255, Math.round(rgb[1])));
        col.rgb.blue = Math.max(0, Math.min(255, Math.round(rgb[2])));
        d.selection.selectAll();
        d.selection.fill(col);
        d.selection.deselect();
        d.convertProfile(dstProfile, intent, bpc, true);
        var cs = d.colorSamplers.add([UnitValue(0, "px"), UnitValue(0, "px")]);
        var c = cs.color.cmyk;
        var out = [c.cyan, c.magenta, c.yellow, c.black];
        cs.remove();
        return out;
    } finally {
        safe(function () { d.close(SaveOptions.DONOTSAVECHANGES); return true; }, false);
    }
}

/*
 * Имя RGB-профиля, которым Photoshop согласен работать.
 *
 * Photoshop адресует профили ТОЧНЫМ названием, а в файле может стоять профиль
 * с названием, которого в системе нет. Скриншоты, например, приходят с ICC
 * по имени «sRGB» — magick его читает, Photoshop открывает файл, а вот
 * convertProfile("sRGB") падает с «The parameters for command Convert to
 * Profile are not currently valid». Поймано на боевом прогоне: три PNG
 * из ста двенадцати.
 *
 * Проверяем пригодность имени единственным способом, который тут работает, —
 * пробуем конвертировать документ 1x1 и смотрим, не бросило ли. Не подошло —
 * берём профиль из настроек (assume_rgb_icc) и говорим об этом вслух.
 */
function usableRgbProfile(doc, job, intent, bpc, warnings) {
    var tries = [];
    var own = safe(function () { return String(doc.colorProfileName); }, "");
    if (job.srcProfile) { tries.push(String(job.srcProfile)); }
    if (own && own !== String(job.srcProfile || "")) { tries.push(own); }
    if (job.rgbProfile) { tries.push(String(job.rgbProfile)); }

    for (var i = 0; i < tries.length; i++) {
        var d = app.documents.add(1, 1, 72, "prepress_probe", NewDocumentMode.RGB,
                                  DocumentFill.WHITE);
        var ok = true;
        try {
            d.convertProfile(tries[i], intent, bpc, true);
        } catch (e) {
            ok = false;
        }
        safe(function () { d.close(SaveOptions.DONOTSAVECHANGES); return true; }, false);
        if (ok) {
            if (i > 0 && own && tries[i] !== own) {
                warnings.push('the file profile "' + own + '" is unknown to Photoshop by name '
                              + '— memory colours measured through "' + tries[i] + '"');
            }
            return tries[i];
        }
    }
    return null;
}

function rgbOfInk(ink, cmykProfile, rgbProfile, intent, bpc) {
    var d = app.documents.add(1, 1, 72, "prepress_probe", NewDocumentMode.RGB,
                              DocumentFill.WHITE);
    try {
        d.changeMode(ChangeMode.CMYK);
        safe(function () { d.colorProfileName = cmykProfile; return true; }, false);
        var col = new SolidColor();
        col.cmyk.cyan = Math.max(0, Math.min(100, ink[0]));
        col.cmyk.magenta = Math.max(0, Math.min(100, ink[1]));
        col.cmyk.yellow = Math.max(0, Math.min(100, ink[2]));
        col.cmyk.black = Math.max(0, Math.min(100, ink[3]));
        d.selection.selectAll();
        d.selection.fill(col);
        d.selection.deselect();
        d.convertProfile(rgbProfile, intent, bpc, true);
        var cs = d.colorSamplers.add([UnitValue(0, "px"), UnitValue(0, "px")]);
        var q = cs.color.rgb;
        var out = [q.red, q.green, q.blue];
        cs.remove();
        return out;
    } finally {
        safe(function () { d.close(SaveOptions.DONOTSAVECHANGES); return true; }, false);
    }
}

/*
 * Значение канала, ниже которого лежит доля q всех пикселей выделения.
 * Нужно для точек чёрного и белого: брать самый тёмный и самый светлый пиксель
 * нельзя — одна пылинка на сканировании или блик на металле сдвинули бы всю
 * кривую. Берётся перцентиль, как это делают пипетки в Photoshop.
 */
function channelPercentile(doc, index, q) {
    var h = doc.channels[index].histogram, total = 0;
    for (var k = 0; k < 256; k++) { total += h[k]; }
    if (!total) { return null; }
    var want = total * q, seen = 0;
    for (var v = 0; v < 256; v++) {
        seen += h[v];
        if (seen >= want) { return v; }
    }
    return 255;
}

function channelMean(doc, index) {
    var h = doc.channels[index].histogram, s = 0, c = 0;
    for (var k = 0; k < 256; k++) { s += k * h[k]; c += h[k]; }
    return c ? s / c : null;
}

// --- детекторы ------------------------------------------------------------
//
// Все они работают ТОЛЬКО по RGB-двойнику (см. пункт 1 в шапке).

function selectSky(doc) {
    // Пустой дескриптор обязателен: с undefined Photoshop отвечает
    // «параметры команды недопустимы» и на документе с небом тоже.
    executeAction(stringIDToTypeID("selectSky"), new ActionDescriptor(), DialogModes.NO);
}

/*
 * Select People. tags — части тела: Photoshop кодирует их целыми числами
 * (10 — кожа лица, 13 — шея, 14 — руки, 15 — кисти, 16 — ноги; проверено
 * перебором на живых кадрах). Строковый список tagsV2, который просится
 * по словарю действий, из ExtendScript падает с program error, а целые
 * числа работают.
 *
 * Список приходит из config.json: нумерация — внутренняя кухня Photoshop,
 * в новой версии она может поехать. Если с тегами ничего не выделилось,
 * вызывающий откатывается на «всех людей целиком».
 */
function selectPeople(doc, tags, personIndex) {
    var d = new ActionDescriptor();
    if (personIndex === null || personIndex === undefined) {
        d.putBoolean(stringIDToTypeID("selectAllPeople"), true);
    } else {
        // Люди нумеруются с единицы; ключ people принимает список номеров.
        var pl = new ActionList();
        pl.putInteger(Number(personIndex));
        d.putList(stringIDToTypeID("people"), pl);
    }
    if (tags && tags.length) {
        var list = new ActionList();
        for (var i = 0; i < tags.length; i++) { list.putInteger(Number(tags[i])); }
        d.putList(stringIDToTypeID("tags"), list);
    }
    executeAction(stringIDToTypeID("selectPeople"), d, DialogModes.NO);
}

// Сколько человек подряд «пусто», прежде чем считать, что люди кончились.
var PEOPLE_GAP = 2;
// Больше этого числа людей в кадре не разбираем: групповое фото из полусотни
// человек считалось бы минутами, а правка каждого была бы неразличима.
var PEOPLE_LIMIT = 12;

function selectSkinRange(doc, fuzziness, detectFaces) {
    var d = new ActionDescriptor();
    d.putInteger(stringIDToTypeID("fuzziness"), fuzziness);
    // Ключ и значения взяты из словаря действий самого Photoshop:
    // colors — перечисление, «тона кожи» в нём зовутся skinTone.
    d.putEnumerated(stringIDToTypeID("colors"), stringIDToTypeID("colors"),
                    stringIDToTypeID("skinTone"));
    // Распознавание лиц сужает диапазон вдвое: без него в кожу попадает
    // всё тёплое — дерево, песок, бежевая стена.
    d.putBoolean(stringIDToTypeID("UseFacesKey"), detectFaces !== false);
    executeAction(stringIDToTypeID("colorRange"), d, DialogModes.NO);
}

function labColor(l, a, b) {
    var d = new ActionDescriptor();
    d.putDouble(charIDToTypeID("Lmnc"), l);
    d.putDouble(charIDToTypeID("A   "), a);
    d.putDouble(charIDToTypeID("B   "), b);
    return d;
}

/*
 * Околонейтральные пиксели: те, что ДОЛЖНЫ быть серыми или белыми.
 *
 * Искать их около a=b=0 нельзя — если на кадре увод, они как раз смещены
 * (в примере заказчика светлые нейтрали стояли на 216/228/231, то есть
 * b около -10). Поэтому коробка в Lab широкая по a и b: пастельные цвета
 * в неё попадают, но кожа (b около +20) и небо (b около -25) — уже нет.
 *
 * Их средний цвет и есть замер увода: насколько разъехались каналы там,
 * где они обязаны совпадать.
 */
function selectNeutralRange(doc, box, fuzziness) {
    selectLabRange(doc, [box[0], -box[2], -box[2]], [box[1], box[2], box[2]], fuzziness);
}

function selectLabRange(doc, labMin, labMax, fuzziness) {
    var d = new ActionDescriptor();
    d.putInteger(stringIDToTypeID("fuzziness"), fuzziness);
    d.putObject(stringIDToTypeID("minimum"), stringIDToTypeID("labColor"),
                labColor(labMin[0], labMin[1], labMin[2]));
    d.putObject(stringIDToTypeID("maximum"), stringIDToTypeID("labColor"),
                labColor(labMax[0], labMax[1], labMax[2]));
    executeAction(stringIDToTypeID("colorRange"), d, DialogModes.NO);
}

/*
 * Области на RGB-двойнике. Возвращает список масок [{name, part}] — пустой,
 * если ничего не нашлось. Ошибка детектора — тоже «нет области»: Select Sky
 * на кадре без неба именно бросает исключение.
 *
 * Кожа возвращает НЕСКОЛЬКО масок — по одной на человека. Среднее по всем
 * лицам сразу лечит плохо: на кадре с тремя людьми у двоих Y-M был 1 и 2
 * (краснота), у третьего 10 (норма), а в среднем выходило 6 — и правка
 * получалась вдвое слабее, чем нужна была двоим. Разбор людей по отдельности
 * стоит дёшево: первый вызов Select People считает модель (9-25 с), каждый
 * следующий берёт готовое (около 0.35 с).
 */
function detectOne(dup, spec) {
    var kind = String(spec.detector || "");
    var masks = [];

    function store(name) {
        var ch = dup.channels.add();
        ch.name = name;
        dup.selection.store(ch);
        dup.activeChannels = dup.componentChannels;
        safe(function () { dup.selection.deselect(); return true; }, false);
    }

    safe(function () { dup.selection.deselect(); return true; }, false);
    dup.activeChannels = dup.componentChannels;

    if (kind === "sky") {
        try { selectSky(dup); } catch (e) { return masks; }
        if (!hasSelection(dup)) { return masks; }
        store("__pp_" + spec.name);
        masks.push({ name: "__pp_" + spec.name, part: "" });
        return masks;
    }

    if (kind === "lab") {
        try {
            selectLabRange(dup, spec.labMin, spec.labMax, spec.fuzziness || 45);
        } catch (e) { return masks; }
        if (!hasSelection(dup)) { return masks; }
        store("__pp_" + spec.name);
        masks.push({ name: "__pp_" + spec.name, part: "" });
        return masks;
    }

    if (kind !== "skin") { return masks; }

    // Дешёвая проверка перед дорогой: цветовой диапазон «Тона кожи» считается
    // доли секунды, разбор людей — секунды. Нет в кадре тёплых тонов — нет
    // и кожи, и модель звать незачем. На пейзаже или синем интерьере это
    // экономит весь этап.
    //
    // Одного цветового диапазона мало: на кадре без единого человека он
    // уверенно отдаёт деревянную стену и бежевый фасад (13.9% «кожи»
    // на фасаде отеля). А пересечение нужно и при точных тегах: оно
    // выбрасывает из замера волосы по краю и глубокую тень, из-за которых
    // средний цвет уезжал в тёмное (31/63/69/50 вместо 10/35/42/3).
    try {
        selectSkinRange(dup, spec.fuzziness || 40, spec.detectFaces);
    } catch (e) { return masks; }
    if (!hasSelection(dup)) { return masks; }
    var warm = dup.channels.add();
    warm.name = "__pp_warm";
    dup.selection.store(warm);
    dup.activeChannels = dup.componentChannels;
    var warmArea = selectionShare(dup);
    safe(function () { dup.selection.deselect(); return true; }, false);
    if (warmArea < Number(spec.minArea || 0)) {
        warm.remove();
        dup.activeChannels = dup.componentChannels;
        return masks;
    }

    function skinOf(personIndex, name) {
        safe(function () { dup.selection.deselect(); return true; }, false);
        var tagged = true;
        try {
            selectPeople(dup, spec.peopleTags, personIndex);
        } catch (te) {
            tagged = false;
        }
        if (tagged && !hasSelection(dup)) { return false; }
        if (!tagged) {
            // Нумерация тегов не сошлась (другая версия Photoshop) —
            // берём людей целиком, дальше их обрежет цветовой диапазон.
            safe(function () { dup.selection.deselect(); return true; }, false);
            try { selectPeople(dup, null, personIndex); } catch (pe) { return false; }
            if (!hasSelection(dup)) { return false; }
        }
        dup.selection.load(warm, SelectionType.INTERSECT);
        dup.activeChannels = dup.componentChannels;
        if (!hasSelection(dup)) { return false; }
        store(name);
        return true;
    }

    if (spec.perPerson === false) {
        if (skinOf(null, "__pp_skin")) { masks.push({ name: "__pp_skin", part: "" }); }
    } else {
        var gap = 0;
        for (var i = 1; i <= PEOPLE_LIMIT && gap < PEOPLE_GAP; i++) {
            var nm = "__pp_skin_" + i;
            if (skinOf(i, nm)) {
                masks.push({ name: nm, part: "человек " + i });
                gap = 0;
            } else {
                gap++;
            }
        }
        // Люди по одному не дались — берём всех сразу, как раньше.
        if (!masks.length && skinOf(null, "__pp_skin")) {
            masks.push({ name: "__pp_skin", part: "" });
        }
    }
    warm.remove();
    dup.activeChannels = dup.componentChannels;
    return masks;
}

// --- кривые ---------------------------------------------------------------

var CHANNEL_ID = {
    "R": "Rd  ", "G": "Grn ", "B": "Bl  ",
    "C": "Cyn ", "M": "Mgnt", "Y": "Yllw", "K": "Blck",
    // Совмещённый канал — им правится светлота, не трогая баланс каналов.
    "RGB_COMPOSITE": "Cmps"
};

/*
 * Кривая по одному каналу. Действует только внутри выделения — на этом
 * и держится вся адресность правки. Точки задаются в значениях канала
 * (0..255), а не в процентах краски.
 */
function applyCurve(doc, chKey, points) {
    var desc = new ActionDescriptor();
    var adjs = new ActionList();
    var chDesc = new ActionDescriptor();
    var ref = new ActionReference();
    ref.putEnumerated(charIDToTypeID("Chnl"), charIDToTypeID("Chnl"),
                      charIDToTypeID(CHANNEL_ID[chKey]));
    chDesc.putReference(charIDToTypeID("Chnl"), ref);
    var pts = new ActionList();
    for (var i = 0; i < points.length; i++) {
        var p = new ActionDescriptor();
        p.putDouble(charIDToTypeID("Hrzn"), points[i][0]);
        p.putDouble(charIDToTypeID("Vrtc"), points[i][1]);
        pts.putObject(charIDToTypeID("Pnt "), p);
    }
    chDesc.putList(charIDToTypeID("Crv "), pts);
    adjs.putObject(charIDToTypeID("CrvA"), chDesc);
    desc.putList(charIDToTypeID("Adjs"), adjs);
    executeAction(charIDToTypeID("Crvs"), desc, DialogModes.NO);
}

/*
 * Кривая «сдвинуть точку и не тронуть концы»: вход остаётся входом на
 * краях диапазона, а замеренное значение уезжает на нужное. Так правка
 * гаснет к светам и теням и не рвёт градиент.
 */
function shiftCurve(inValue, outValue) {
    var x = Math.max(1, Math.min(254, inValue));
    var y = Math.max(0, Math.min(255, outValue));
    return [[0, 0], [x, y], [255, 255]];
}

/*
 * Готовые авто-алгоритмы самого Photoshop.
 *
 * Всё то же, что делает correctTone руками, но отточенное Adobe за двадцать
 * лет. Четыре режима у события levels:
 *   auto            — поканальный контраст: растягивает каждый канал отдельно,
 *                     этим и снимает увод («Enhance Per Channel Contrast»);
 *   autoContrast    — тот же растяг, но общий для всех каналов: светлота
 *                     меняется, цвет нет;
 *   autoBlackWhite  — «найти тёмные и светлые цвета»: точки ставятся
 *                     по реальным цветам кадра, а не по крайним значениям;
 *   autoNeutrals    — «привязать средние тона к нейтрали» — серая точка.
 *
 * Рекомендация Adobe и наш выбор по умолчанию — последние два вместе.
 * Чисел они не возвращают, поэтому «до» и «после» замеряем сами.
 */
function levelsAuto(doc, modes) {
    var d = new ActionDescriptor();
    var r = new ActionReference();
    r.putEnumerated(charIDToTypeID("Lyr "), charIDToTypeID("Ordn"), charIDToTypeID("Trgt"));
    d.putReference(charIDToTypeID("null"), r);
    d.putEnumerated(stringIDToTypeID("presetKind"), stringIDToTypeID("presetKindType"),
                    stringIDToTypeID("presetKindDefault"));
    var any = false;
    for (var i = 0; i < modes.length; i++) {
        d.putBoolean(stringIDToTypeID(String(modes[i])), true);
        any = true;
    }
    if (!any) { return false; }
    executeAction(stringIDToTypeID("levels"), d, DialogModes.NO);
    return true;
}

/*
 * Общий тон готовым алгоритмом. Возвращает строку для отчёта.
 *
 * Ворота остаются нашими: алгоритм Adobe не знает ни про небо, ни про закат,
 * ни про то, что кадр может быть задуман тёмным. Поэтому решение «править ли
 * вообще» принимаем до него — по тем же числам, что и раньше, — а саму правку
 * отдаём ему.
 */
function correctToneAdobe(doc, opts, warnings) {
    var before = [channelMean(doc, 0), channelMean(doc, 1), channelMean(doc, 2)];
    if (before[0] === null) { return ""; }
    var modes = opts.adobeModes || ["autoBlackWhite", "autoNeutrals"];
    var ok = true;
    try {
        ok = levelsAuto(doc, modes);
    } catch (e) {
        warnings.push("Photoshop auto levels failed: " + errText(e));
        return "";
    }
    if (!ok) { return ""; }
    var after = [channelMean(doc, 0), channelMean(doc, 1), channelMean(doc, 2)];
    var moved = Math.abs(after[0] - before[0]) + Math.abs(after[1] - before[1])
              + Math.abs(after[2] - before[2]);
    if (moved < 1.5) { return ""; }
    return ("авто-уровни Photoshop (" + modes.join(" + ") + "): среднее "
            + roundArr(before) + " -> " + roundArr(after));
}

// --- общий тон: чёрная, серая и белая точки, экспозиция ------------------
//
// То, что ретушёр делает тремя пипетками. Порядок важен: общий увод снимается
// ПЕРВЫМ, до памятных цветов. Иначе правила по коже и небу борются не с местным
// цветом, а с общим уводом кадра, и тянут каналы не туда.

/*
 * Кривая по трём точкам: чёрная, серая, белая. Точки, совпавшие со своей
 * целью, просто не двигаются — кривая от этого не портится.
 */
function tonePoints(lo, loTo, mid, midTo, hi, hiTo) {
    var pts = [];
    function add(x, y) {
        x = Math.max(0, Math.min(255, x));
        y = Math.max(0, Math.min(255, y));
        // Кривая Photoshop требует строго возрастающих входов — и не больше
        // ШЕСТНАДЦАТИ точек: на семнадцатой команда падает с «program error».
        if (pts.length >= 16) { return; }
        if (pts.length && x <= pts[pts.length - 1][0]) { return; }
        pts.push([x, y]);
    }
    // Между опорными точками кривую ведём отрезками, подставляя промежуточные
    // точки. Без них Photoshop гнёт сплайн, и он ПРОВИСАЕТ: на примере
    // заказчика белая точка ушла с 214 на 248, а середина кадра от этого
    // просела со 142 до 127 — картинка стала темнее, хотя её никто
    // не просил темнеть.
    function segment(x0, y0, x1, y1) {
        var steps = 3;
        for (var i = 1; i <= steps; i++) {
            var t = i / steps;
            add(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t);
        }
    }
    add(0, 0);
    var px = 0, py = 0;
    if (lo > 0) { segment(px, py, lo, loTo); px = lo; py = loTo; }
    if (mid !== null && mid > px) { segment(px, py, mid, midTo); px = mid; py = midTo; }
    if (hi > px) { segment(px, py, hi, hiTo); px = hi; py = hiTo; }
    if (px < 255) { segment(px, py, 255, 255); }
    return pts;
}

/*
 * Сдвиг точки: сначала ослабляется коэффициентом strength, потом режется
 * пределом. Оба ограничителя нужны и оба появились из брака.
 *
 * strength — потому что «правильные» числа не всегда правда. Контровой свет,
 * закат, тёплая лампа: увод там не ошибка съёмки, а сам свет, и вытягивание
 * его в ноль убивает кадр. Половинная правка снимает грязь и оставляет свет.
 *
 * limit — потому что большой сдвиг РВЁТ цвет: растянуть узкую гистограмму
 * на весь диапазон значит получить постеризацию, а поканально неравный
 * растяг — ещё и цветной сдвиг (белая 212/223/229 -> 248/248/248 выглядела
 * в оттиске рваной).
 */
/*
 * Цель, разложенная на светлоту и цвет.
 *
 * Поднять тусклые света — безопасно и почти всегда на пользу. А вот свести
 * каналы в нейтраль — операция с ценой: на солнечном кадре «увод» это и есть
 * тёплый свет, и вытягивая белую рубашку в нейтральный белый, мы заодно
 * выхолаживаем небо и траву. Поэтому светлота правится целиком, а цвет —
 * долей colorStrength.
 */
function splitTarget(from, goalAvg, avgFrom, limit, colorLimit, strength, colorStrength) {
    // Светлота — общий для всех каналов подъём; цвет — отклонение канала
    // от среднего. У каждой части свой предел и своё колено, иначе крупный
    // подъём светлоты съедает всю цветовую правку заодно с собой.
    var light = limit * tanh(((goalAvg - avgFrom) * strength) / limit);
    var color = colorLimit * tanh(((avgFrom - from) * strength * colorStrength) / colorLimit);
    return from + light + color;
}

/* Гиперболический тангенс: в ExtendScript его нет, а нужен. */
function tanh(x) {
    if (x > 20) { return 1; }
    if (x < -20) { return -1; }
    var e = Math.exp(2 * x);
    return (e - 1) / (e + 1);
}

function clampShift(from, to, limit, strength) {
    var d = (to - from) * (strength === undefined ? 1 : strength);
    // МЯГКОЕ КОЛЕНО, а не обрезка по пределу.
    //
    // Мелкая правка полезна, крупная ломает — это видно на оттисках. Белая
    // точка, поднятая на 16, оживила кадр; поднятая на 36 — дала постеризацию
    // и цветной сдвиг. Чёрная, сведённая к нейтрали на 25 значений, сделала
    // тёплые тени контрового света синими.
    //
    // Линейное ослабление тут не годится: оно одинаково режет и полезное,
    // и вредное — удачные кадры теряли выигрыш вместе с неудачными. Колено
    // пропускает малое почти целиком (5 из 18 -> 4.9) и насыщает большое
    // (36 -> 17.4), никогда не переходя предел.
    return from + limit * tanh(d / limit);
}

/*
 * Замер и правка общего тона. Работает по ВСЕМУ кадру (RGB-документ),
 * до памятных цветов. Возвращает строку-описание для отчёта или "".
 *
 * Что делает:
 *   1. Точки чёрного и белого — по перцентилям каждого канала. Света,
 *      не дотянутые до белого, тянутся вверх; завышенные тени опускаются.
 *      Поканально — именно это и снимает увод (в примере заказчика света
 *      стояли 222/232/236, то есть синили, и ручная правка вывела их в 255).
 *   2. Серая точка — по околонейтральным пикселям: их каналы сводятся
 *      к общему среднему. Это снимает остаток увода в середине тона.
 *   3. Экспозиция — если КОЖА темнее порога, средние тона поднимаются
 *      гаммой. Именно кожа, а не общая медиана: тёмный кадр может быть
 *      задуман, а тёмное лицо — почти никогда.
 */
function correctTone(doc, opts, skinMasks, skyMask, warnings) {
    var notes = [];
    var n = componentCount(doc);
    if (n < 3) { return ""; }

    var loQ = Number(opts.shadowPercentile || 0.005);
    var lo = [];
    for (var i = 0; i < 3; i++) {
        lo.push(channelPercentile(doc, i, loQ));
        if (lo[i] === null) { return ""; }
    }
    var loFromNeutrals = false;

    // --- околонейтральные: по ним ставятся и белая, и серая точки ---------
    //
    // Белую точку берём именно по НИМ, а не по самым ярким пикселям кадра.
    // Пипетка ретушёра тычет в белую рубашку, а не в блик на бокале: яркие
    // пиксели могут быть насыщенным цветом (жёлтая стена в солнце), и тянуть
    // их каналы к общей цели значит выбелить сам цвет.
    var neutral = null, neutralHi = null, neutralArea = 0;
    try {
        safe(function () { doc.selection.deselect(); return true; }, false);
        selectNeutralRange(doc, opts.neutralBox || [45, 97, 15],
                           Number(opts.neutralFuzziness || 25));
        /*
         * Небо вычитаем. Бледное небо лежит в той же коробке Lab, что и
         * нейтрали (b около -10 при допуске 15), и на пейзаже занимает
         * половину кадра — становясь «эталоном серого». Серая точка после
         * этого обесцвечивает само небо: на кадре с гольфистами оно уехало
         * из 210/220/226 в 207/214/229, то есть в сиреневое.
         *
         * Небу нейтральным быть и не положено: у него свой памятный цвет
         * и свои правила.
         */
        if (skyMask && hasSelection(doc)) {
            var skyCh = null;
            try { skyCh = doc.channels.getByName(skyMask); } catch (e) { skyCh = null; }
            if (skyCh !== null) {
                safe(function () {
                    // Вычитание выделения зовётся DIMINISH. SUBTRACT, который
                    // просится по смыслу, в объектной модели отсутствует —
                    // и ошибка молча гасится, как это и случилось в первый раз.
                    doc.selection.load(skyCh, SelectionType.DIMINISH); return true; }, false);
                doc.activeChannels = doc.componentChannels;
            }
        }
        if (hasSelection(doc)) {
            neutralArea = selectionShare(doc);
            if (neutralArea >= Number(opts.neutralMinArea || 1.5)) {
                neutral = [channelMean(doc, 0), channelMean(doc, 1), channelMean(doc, 2)];
                var hq = Number(opts.neutralHighlightPercentile || 0.95);
                neutralHi = [channelPercentile(doc, 0, hq),
                             channelPercentile(doc, 1, hq),
                             channelPercentile(doc, 2, hq)];
            }
        }
    } catch (e) {
        neutral = null;
    }
    safe(function () { doc.selection.deselect(); return true; }, false);
    doc.activeChannels = doc.componentChannels;

    /*
     * Чёрная точка — ТОЛЬКО по тёмным нейтралям, как белая по светлым.
     *
     * Поканальный перцентиль всего кадра для этого не годится: его задают
     * не тени, а насыщенные предметы. На кадре с гольфистами он дал 27/3/2 —
     * это не тень, это красные брюки: в канале R они яркие, в G и B тёмные.
     * Сведение такой «тени» к нейтрали поднимает зелёное с синим и гасит
     * красное, и кожа в тени зеленеет. На другом кадре тем же способом
     * тёплые тени контрового света стали синими.
     *
     * Нет в кадре тёмных нейтралей — чёрную точку не трогаем вовсе.
     */
    var darkBox = opts.darkBox || [4, 34, 12];
    try {
        safe(function () { doc.selection.deselect(); return true; }, false);
        selectNeutralRange(doc, darkBox, Number(opts.neutralFuzziness || 25));
        if (skyMask && hasSelection(doc)) {
            var dsky = null;
            try { dsky = doc.channels.getByName(skyMask); } catch (e) { dsky = null; }
            if (dsky !== null) {
                safe(function () {
                    doc.selection.load(dsky, SelectionType.DIMINISH); return true; }, false);
                doc.activeChannels = doc.componentChannels;
            }
        }
        if (hasSelection(doc)) {
            var darkArea = selectionShare(doc);
            if (darkArea >= Number(opts.darkMinArea || 0.5)) {
                var dm = [channelMean(doc, 0), channelMean(doc, 1), channelMean(doc, 2)];
                if (dm[0] !== null) { lo = dm; loFromNeutrals = true; }
            }
        }
    } catch (e) {
        loFromNeutrals = false;
    }
    safe(function () { doc.selection.deselect(); return true; }, false);
    doc.activeChannels = doc.componentChannels;

    var hiTarget = Number(opts.highlightTarget || 248);
    var loTarget = Number(opts.shadowTarget || 10);
    var limit = Number(opts.maxShift || 18);
    // Тени отдельно и строже: там сидит свет сюжета. На контровом кадре
    // тени стояли 35/35/3 — тёплые, — и требование нейтрали вынуло из них
    // красное с зелёным, оставив синее. Чёрный стал синим.
    var loLimit = Number(opts.shadowMaxShift || 8);
    var strength = Number(opts.strength === undefined ? 0.5 : opts.strength);
    var minMove = Number(opts.minMove || 3);
    var keys = ["R", "G", "B"];
    var touched = false;

    if (neutral === null) {
        // Нейтралей в кадре нет — закат, монохром, цветной свет. Поканальная
        // правка тут убила бы замысел: она свела бы цвет сюжета к серому.
        // Трогаем только светлоту, одинаково по всем каналам.
        var compHi = channelPercentile(doc, 0, 1 - Number(opts.highlightPercentile || 0.005));
        var compTo = clampShift(compHi, hiTarget, limit, strength);
        if (Math.abs(compTo - compHi) >= minMove) {
            if (opts.apply !== false) {
                applyCurve(doc, "RGB_COMPOSITE",
                           tonePoints(lo[0], lo[0], null, null, compHi, compTo));
            }
            touched = true;
            notes.push("нейтралей в кадре нет (" + Math.round(neutralArea * 10) / 10
                       + "%) — цвет не трогаем, только светлота: света "
                       + compHi + " -> " + Math.round(compTo));
        }
    } else {
        var neutralAvg = (neutral[0] + neutral[1] + neutral[2]) / 3;

        // Цель для белой точки — НЕ НИЖЕ того, что уже есть: если света стоят
        // на 254, опускать их до 248 значит положить краску туда, где была
        // чистая бумага. Каналы при этом всё равно сводятся в одну точку,
        // то есть увод снимается, а яркость светов не теряется.
        var hiGoal = Math.max(hiTarget,
                              Math.max(neutralHi[0], Math.max(neutralHi[1], neutralHi[2])));

        /*
         * И НЕ ВЫШЕ, чем позволяют собственные света кадра.
         *
         * Белая точка ставится по нейтралям, но кривая действует на всё,
         * и то, что лежит ВЫШЕ опорной точки, прижимается к потолку.
         * На кадре с облаками нейтрали (рубашка, флаг) стояли на 220, а сами
         * облака — на 246: подъём опоры до 239 сжал 220..246 в 239..255,
         * и облака потеряли половину контраста (разброс 10 -> 6), превратившись
         * в белое пятно.
         *
         * Поэтому подъём ограничен так, чтобы самая светлая точка КАДРА
         * (перцентиль highlightPercentile) не ушла выше ceiling.
         */
        var ceiling = Number(opts.highlightCeiling || 252);
        var imgHi = channelPercentile(doc, 0, 1 - Number(opts.highlightPercentile || 0.005));
        for (var ih = 1; ih < 3; ih++) {
            var v = channelPercentile(doc, ih, 1 - Number(opts.highlightPercentile || 0.005));
            if (v !== null && (imgHi === null || v > imgHi)) { imgHi = v; }
        }
        if (imgHi !== null) {
            var room = ceiling - imgHi;
            var hiAvgNow = (neutralHi[0] + neutralHi[1] + neutralHi[2]) / 3;
            if (room < hiGoal - hiAvgNow) {
                hiGoal = hiAvgNow + Math.max(0, room);
            }
        }
        // Чёрную точку, наоборот, сводим к среднему каналов: поднять её
        // до цели значило бы вымыть чёрный фон, а опустить до минимума —
        // задавить деталь в тенях. Выше loTarget не поднимаем.
        var loGoal = Math.min(loTarget, (lo[0] + lo[1] + lo[2]) / 3);

        var hiTo = [], loTo = [], midTo = [];
        var cs = Number(opts.colorStrength === undefined ? 0.6 : opts.colorStrength);
        var hiAvg = (neutralHi[0] + neutralHi[1] + neutralHi[2]) / 3;
        var loAvg = (lo[0] + lo[1] + lo[2]) / 3;
        var colorLimit = Number(opts.colorMaxShift || 12);
        for (var c = 0; c < 3; c++) {
            hiTo.push(splitTarget(neutralHi[c], hiGoal, hiAvg,
                                  limit, colorLimit, strength, cs));
            loTo.push(loFromNeutrals
                      ? splitTarget(lo[c], loGoal, loAvg, loLimit, colorLimit, strength, cs)
                      : lo[c]);
            // Серая точка — операция чисто цветовая, светлоты в ней нет.
            midTo.push(clampShift(neutral[c], neutralAvg, colorLimit, strength * cs));
            if (Math.abs(hiTo[c] - neutralHi[c]) >= minMove
                || Math.abs(loTo[c] - lo[c]) >= minMove
                || Math.abs(midTo[c] - neutral[c]) >= minMove) {
                touched = true;
            }
        }
        if (touched) {
            if (opts.apply !== false) {
                for (var q = 0; q < 3; q++) {
                    applyCurve(doc, keys[q],
                               tonePoints(lo[q], loTo[q], neutral[q], midTo[q],
                                          neutralHi[q], hiTo[q]));
                }
            }
            notes.push("нейтралей " + Math.round(neutralArea * 10) / 10
                       + "% кадра; белая точка " + roundArr(neutralHi) + " -> "
                       + roundArr(hiTo) + ", серая " + roundArr(neutral) + " -> "
                       + roundArr(midTo)
                       + (loFromNeutrals
                          ? (", чёрная по тёмным нейтралям " + roundArr(lo) + " -> " + roundArr(loTo))
                          : ", тёмных нейтралей нет — чёрную точку не трогаем"));
        }
    }

    // --- экспозиция по коже ---
    var expo = opts.exposure || {};
    if (expo.enabled !== false && (!skinMasks || !skinMasks.length)) {
        // Молчать тут нельзя: экспозиция меряется по маскам кожи, и если
        // их не строили (выключён memory_colors.skin) или людей в кадре нет,
        // этап просто ничего не делает — а выглядело бы это как «всё в норме».
        notes.push("кожи в кадре нет — экспозицию не трогаем");
    }
    if (expo.enabled !== false && skinMasks && skinMasks.length) {
        var sum = 0, cnt = 0;
        for (var s2 = 0; s2 < skinMasks.length; s2++) {
            var ch = null;
            try { ch = doc.channels.getByName(skinMasks[s2]); } catch (e) { ch = null; }
            if (ch === null) { continue; }
            doc.selection.load(ch, SelectionType.REPLACE);
            doc.activeChannels = doc.componentChannels;
            /*
             * Светлота ОСВЕЩЁННОЙ кожи, а не средняя по маске.
             *
             * На контровом кадре в маску попадают и лица, и затенённые руки
             * с ногами; среднее тянут вниз они, и подъём получает кадр,
             * у которого лица сняты нормально. В оттиске это выглядело как
             * выцветшая трава и плоские белые лица.
             *
             * Перцентиль отвечает на правильный вопрос: «темна ли кожа ТАМ,
             * ГДЕ НА НЕЁ ПАДАЕТ СВЕТ». Тени при этом поднимутся заодно —
             * кривая всё равно общая.
             */
            if (hasSelection(doc)) {
                var q = Number(expo.litPercentile || 0.75);
                var lit = 0, got = 0;
                for (var lc = 0; lc < 3; lc++) {
                    var v = channelPercentile(doc, lc, q);
                    if (v !== null) { lit += v; got++; }
                }
                if (got === 3) { sum += lit / 3; cnt++; }
            }
            safe(function () { doc.selection.deselect(); return true; }, false);
        }
        if (cnt) {
            var skinL = sum / cnt;
            var want = Number(expo.skinMin || 120);
            if (skinL < want) {
                /*
                 * Точка ставится ТАМ, ГДЕ КОЖА, и тянется до порога. Выше
                 * ставится вторая точка — держать света.
                 *
                 * Так делают руками, и на контровом свете только так и можно:
                 * общая гамма подняла бы вместе с лицами и залитый солнцем
                 * фон, а он и без того на пределе. Держащая точка берётся
                 * по светлой части кадра (перцентиль holdPercentile), то есть
                 * по тому, что уже яркое, и оно остаётся на месте.
                 */
                var lift = Math.min(Number(expo.maxLift || 25), (want - skinL) * strength);
                var hold = channelPercentile(doc, 0, Number(expo.holdPercentile || 0.9));
                if (hold === null || hold <= skinL + lift + 10) { hold = 255; }
                var pts = [[0, 0]];
                var sx = Math.max(1, Math.round(skinL));
                var sy = Math.min(254, Math.round(skinL + lift));
                pts.push([Math.round(sx / 2), Math.round(sy / 2)]);   // тени тянутся вместе
                pts.push([sx, sy]);
                if (hold < 255) { pts.push([Math.round(hold), Math.round(hold)]); }
                pts.push([255, 255]);
                if (opts.apply !== false) {
                    applyCurve(doc, "RGB_COMPOSITE", pts);
                }
                touched = true;
                notes.push("кожа темна (" + Math.round(skinL) + " при пороге " + want
                           + ") — тени подняты на " + Math.round(lift)
                           + (hold < 255 ? (", света удержаны точкой " + Math.round(hold)) : ""));
            } else {
                notes.push("кожа по светлоте в норме (" + Math.round(skinL) + ")");
            }
        }
    }
    doc.activeChannels = doc.componentChannels;
    return touched ? notes.join("; ") : "";
}

function roundArr(a) {
    var out = [];
    for (var i = 0; i < a.length; i++) { out.push(a[i] === null ? "-" : Math.round(a[i])); }
    return out.join("/");
}

// --- правила --------------------------------------------------------------

var INK_INDEX = { "C": 0, "M": 1, "Y": 2, "K": 3 };

// Сколько заходов на доводку. Три хватает с запасом: первый снимает
// основное, второй добирает остаток, третий обычно упирается в порог
// «ближе печать не покажет» и выходит сам.
var CORRECTION_PASSES = 3;

function ruleValue(ink, rule) {
    var v = ink[INK_INDEX[rule.ch]];
    if (rule.kind === "ratio") {
        var ref = ink[INK_INDEX[rule.ref]];
        if (ref <= 0.5) { return null; }   // делить на почти-ноль бессмысленно
        return v / ref;
    }
    if (rule.kind === "diff") { return v - ink[INK_INDEX[rule.ref]]; }
    return v;
}

/* Значение канала (в процентах краски), при котором правило выполнено ровно. */
function inkForBound(ink, rule, bound) {
    if (rule.kind === "ratio") { return ink[INK_INDEX[rule.ref]] * bound; }
    if (rule.kind === "diff") { return ink[INK_INDEX[rule.ref]] + bound; }
    return bound;
}

/*
 * Ворота: область не похожа на то, чем её назвал детектор. Select Sky
 * отдаёт белый потолок в интерьере и туманное море, цветовой диапазон
 * зелени — сукно бильярдного стола. Ворота смотрят на сами числа
 * и молча отсеивают такие области.
 */
function gatesFailed(ink, gates) {
    for (var i = 0; i < (gates || []).length; i++) {
        var g = gates[i];
        var v = ruleValue(ink, g);
        if (v === null) { return g; }
        if (g.min !== undefined && g.min !== null && v < g.min) { return g; }
        if (g.max !== undefined && g.max !== null && v > g.max) { return g; }
    }
    return null;
}

/*
 * Что нужно поправить. Возвращает список {ch, from, to, rule, was, bound}:
 * канал тянется до БЛИЖАЙШЕЙ нарушенной границы, а не к середине нормы —
 * кадр, снятый правильно, не должен меняться вовсе.
 */
function planCorrection(ink, rules, maxShift, strength) {
    var plan = [];
    for (var i = 0; i < (rules || []).length; i++) {
        var r = rules[i];
        var v = ruleValue(ink, r);
        if (v === null) { continue; }
        var bound = null;
        // Прицел: ведём к середине диапазона ВСЕГДА, а не только при выходе
        // за границу. На реальном номере ограничитель сдвинул медиану Y минус M
        // с 2.8 до 6.0 и оставил 78% лиц вне нормы 8-15, которую сам и проверяет.
        if (r.aim !== undefined && r.aim !== null) { bound = r.aim; }
        else if (r.min !== undefined && r.min !== null && v < r.min) { bound = r.min; }
        else if (r.max !== undefined && r.max !== null && v > r.max) { bound = r.max; }
        if (bound === null) { continue; }

        var idx = INK_INDEX[r.ch];
        var want = inkForBound(ink, r, bound);
        want = Math.max(0, Math.min(100, want));
        // Не до самой границы, а частью пути. Правило описывает, где норма
        // КОНЧАЕТСЯ, а не куда надо попасть: дотягивать вплотную значит
        // ровнять все кадры под один трафарет. На зелени это вылезало сразу —
        // у травы в тени пурпурной закономерно больше, и её выравнивание
        // делало зелень холодной, «изумрудной» вместо луговой.
        var delta = (want - ink[idx]) * (strength === undefined ? 1 : strength);
        want = ink[idx] + delta;
        if (Math.abs(delta) > maxShift) {
            delta = (delta > 0 ? 1 : -1) * maxShift;
            want = ink[idx] + delta;
        }
        if (Math.abs(delta) < 0.5) { continue; }   // ниже полупроцента печать не покажет
        plan.push({ ch: r.ch, from: ink[idx], to: want, was: v, bound: bound, rule: r });
    }
    return plan;
}

function inkString(ink) {
    return Math.round(ink[0]) + "/" + Math.round(ink[1]) + "/"
         + Math.round(ink[2]) + "/" + Math.round(ink[3]);
}

// --- один файл ------------------------------------------------------------

function processJob(job) {
    var warnings = [];
    var findings = [];

    function finding(pairs) { findings.push(jsObj(pairs)); }
    function done(extra) {
        return "{" + [
            '"src":' + jsVal(String(job.src)),
            '"dst":' + jsVal(String(job.dst)),
            '"findings":[' + findings.join(",") + "]",
            '"warnings":' + jsVal(warnings.join("|")),
            '"timings":' + jsVal(tdump()),
            extra
        ].join(",") + "}";
    }

    var src = new File(String(job.src));
    if (!src.exists) {
        return done('"ok":false,"changed":false,"error":' + jsVal("file not found on disk"));
    }

    var doc = null, dup = null;
    tstart();
    try {
        doc = app.open(src);
        tmark("открытие");
    } catch (e) {
        return done('"ok":false,"changed":false,"error":'
                    + jsVal("Photoshop did not open the file: " + errText(e)));
    }

    try {
        var mode = String(doc.mode);
        var isCMYK = safe(function () { return doc.mode === DocumentMode.CMYK; }, false);
        var isRGB = safe(function () { return doc.mode === DocumentMode.RGB; }, false);
        if (!isCMYK && !isRGB) {
            doc.close(SaveOptions.DONOTSAVECHANGES);
            return done('"ok":true,"changed":false,"mode":' + jsVal(mode)
                        + ',"skipped":' + jsVal(mode + " mode — memory colours are not looked for") + ',"error":""');
        }

        // Исходный профиль назначаем, только если своего в файле нет:
        // та же развилка, что у magick с ключом -profile.
        if (isRGB && job.srcProfile) {
            safe(function () { doc.colorProfileName = String(job.srcProfile); return true; }, false);
        }
        if (safe(function () { return doc.layers.length; }, 1) > 1) {
            safe(function () { doc.flatten(); return true; }, false);
        }

        /*
         * Правок на кадр накладывается много: общий тон по трём каналам,
         * потом кожа по каждому человеку, небо, зелень — и каждая кривая
         * квантует восемь бит заново. В оттиске это вылезало «рваным» цветом
         * на плавных переходах: небо, щёки, зелёный газон.
         *
         * Поэтому на время правки документ переводится в 16 бит и возвращается
         * в 8 перед записью. Память и время это стоит, постеризация — дороже.
         */
        var was8bit = safe(function () {
            return doc.bitsPerChannel === BitsPerChannelType.EIGHT; }, false);
        if (was8bit) {
            safe(function () {
                doc.bitsPerChannel = BitsPerChannelType.SIXTEEN; return true; }, false);
        }

        var fullW = doc.width.as("px"), fullH = doc.height.as("px");
        var intent = intentByName(job.intent);
        var bpc = job.blackPointCompensation !== false;

        // Одно имя профиля на весь файл: и замер, и обратный перевод должны
        // ходить одним и тем же кругом, иначе разница кругов подмешается
        // в правку.
        var rgbName = isRGB ? usableRgbProfile(doc, job, intent, bpc, warnings) : "";
        tmark("профиль");
        if (isRGB && rgbName === null) {
            doc.close(SaveOptions.DONOTSAVECHANGES);
            return done('"ok":false,"changed":false,"error":'
                        + jsVal("Photoshop knows no suitable RGB profile by name — "
                                + "there is nothing to measure memory colours in"));
        }

        // --- RGB-двойник под детект ---------------------------------------
        // Детект уменьшенной копии — не экономия впрок: Select Sky на
        // 34 Мпикс идёт секундами, а маску всё равно сгладит обратный
        // ресайз. Сама картинка при этом не трогается: двойник нужен
        // только ради масок.
        dup = doc.duplicate();
        tmark("двойник");
        if (isCMYK) {
            dup.convertProfile(String(job.rgbProfile), intent, bpc, true);
        }
        var detectPx = Number(job.detectPx || 2000);
        var longSide = Math.max(fullW, fullH);
        if (detectPx > 0 && longSide > detectPx) {
            if (fullW >= fullH) {
                dup.resizeImage(UnitValue(detectPx, "px"), null, null, ResampleMethod.BICUBIC);
            } else {
                dup.resizeImage(null, UnitValue(detectPx, "px"), null, ResampleMethod.BICUBIC);
            }
        }

        tmark("уменьшение двойника");
        var colors = job.colors || [];
        var masks = [];
        for (var i = 0; i < colors.length; i++) {
            var spec = colors[i];
            var found = detectOne(dup, spec);
            tmark("детект " + spec.name);
            if (!found.length) {
                finding(["color", String(spec.name), "part", "", "status", "none",
                         "why", "область не найдена", "area", 0,
                         "before", "", "after", "", "shift", ""]);
                continue;
            }
            for (var f2 = 0; f2 < found.length; f2++) {
                masks.push({ spec: spec, name: found[f2].name, part: found[f2].part });
            }
        }

        // Маски переезжают в рабочий документ каналами: выделение
        // duplicate() не переносит, а канал переносит.
        if (masks.length) {
            dup.resizeImage(UnitValue(fullW, "px"), UnitValue(fullH, "px"),
                            null, ResampleMethod.BICUBIC);
            tmark("маски обратно в размер");
            for (var m = 0; m < masks.length; m++) {
                app.activeDocument = dup;
                dup.channels.getByName(masks[m].name).duplicate(doc);
            }
            tmark("перенос масок")
        }
        app.activeDocument = doc;
        dup.close(SaveOptions.DONOTSAVECHANGES);
        dup = null;
        doc.activeChannels = doc.componentChannels;

        var totalPx = fullW * fullH;
        var changed = false;

        // --- общий тон: чёрная, серая и белая точки, экспозиция -------------
        // Идёт ПЕРВЫМ: пока на кадре общий увод, правила по коже и небу
        // борются с ним, а не с местным цветом.
        var tone = job.tone || null;
        if (tone && tone.enabled !== false && isRGB) {
            var skinNames = [];
            for (var sm = 0; sm < masks.length; sm++) {
                if (String(masks[sm].spec.name) === "skin") { skinNames.push(masks[sm].name); }
            }
            var skyName = null;
            for (var sk = 0; sk < masks.length; sk++) {
                if (String(masks[sk].spec.name) === "sky") { skyName = masks[sk].name; }
            }
            var toneNote = "";
            // Неизвестное имя движка — это ошибка вызывающей стороны, и молчать
            // о ней нельзя: однажды сюда приходило слово "auto" (его резолвит
            // питон), скрипт тихо откатывался на свою арифметику, и вариант
            // «всё на Adobe» правил тон вовсе не Adobe. Сбоя при этом не было
            // никакого — только строчка в логе, если её заметить.
            var toneEngine = String(tone.engine || "own");
            if (toneEngine !== "own" && toneEngine !== "photoshop") {
                warnings.push('overall tone: engine "' + toneEngine
                              + '" неизвестен, взята своя арифметика');
                toneEngine = "own";
            }
            try {
                toneNote = (toneEngine === "photoshop")
                    ? correctToneAdobe(doc, tone, warnings)
                    : correctTone(doc, tone, skinNames, skyName, warnings);
            } catch (te) {
                warnings.push("overall tone not corrected: " + errText(te));
            }
            tmark("общий тон");
            if (toneNote) {
                if (tone.apply !== false && job.apply !== false) { changed = true; }
                finding(["color", "tone", "part", "", "status",
                         (tone.apply !== false && job.apply !== false) ? "corrected" : "would",
                         "why", toneNote, "area", 100,
                         "before", "", "after", "", "shift", ""]);
            } else {
                finding(["color", "tone", "part", "", "status", "ok",
                         "why", "общий тон в норме", "area", 100,
                         "before", "", "after", "", "shift", ""]);
            }
        }

        for (var j = 0; j < masks.length; j++) {
            var spec = masks[j].spec;
            var chName = masks[j].name;
            var part = masks[j].part || "";
            var ch = null;
            try { ch = doc.channels.getByName(chName); } catch (e) { ch = null; }
            if (ch === null) {
                finding(["color", String(spec.name), "part", part, "status", "error",
                         "why", "маска не доехала до рабочего документа", "area", 0,
                         "before", "", "after", "", "shift", ""]);
                continue;
            }

            doc.selection.load(ch, SelectionType.REPLACE);
            doc.activeChannels = doc.componentChannels;
            var st = hasSelection(doc) ? meanChannels(doc) : null;
            tmark("замер " + spec.name);
            if (st === null) {
                ch.remove();
                doc.activeChannels = doc.componentChannels;
                finding(["color", String(spec.name), "part", part, "status", "none",
                         "why", "область не найдена", "area", 0,
                         "before", "", "after", "", "shift", ""]);
                continue;
            }

            var area = st.count / totalPx * 100;
            var minArea = Number(spec.minArea || 0);
            if (area < minArea) {
                ch.remove();
                doc.activeChannels = doc.componentChannels;
                safe(function () { doc.selection.deselect(); return true; }, false);
                finding(["color", String(spec.name), "part", part, "status", "small",
                         "why", "область меньше " + minArea + "% кадра",
                         "area", round1(area), "before", "", "after", "", "shift", ""]);
                continue;
            }

            // Печатные числа области: в CMYK-файле они лежат прямо в
            // каналах, в RGB-файле их считает ICC на документе 1x1.
            var ink, srcRgb = null;
            if (isCMYK) {
                ink = [inkFromChannel(st.means[0]), inkFromChannel(st.means[1]),
                       inkFromChannel(st.means[2]), inkFromChannel(st.means[3])];
            } else {
                srcRgb = [st.means[0], st.means[1], st.means[2]];
                ink = inkOfRgb(srcRgb, rgbName,
                               String(job.targetProfile), intent, bpc);
                tmark("ICC " + spec.name);
            }

            var bad = gatesFailed(ink, spec.gates);
            if (bad !== null) {
                ch.remove();
                doc.activeChannels = doc.componentChannels;
                safe(function () { doc.selection.deselect(); return true; }, false);
                finding(["color", String(spec.name), "part", part, "status", "unlike",
                         "why", "по числам не похоже на «" + spec.name + "»",
                         "area", round1(area), "before", inkString(ink),
                         "after", "", "shift", ""]);
                continue;
            }

            var plan = planCorrection(ink, spec.rules, Number(job.maxShift || 12),
                                      Number(job.strength === undefined ? 1 : job.strength));
            if (!plan.length) {
                ch.remove();
                doc.activeChannels = doc.componentChannels;
                safe(function () { doc.selection.deselect(); return true; }, false);
                finding(["color", String(spec.name), "part", part, "status", "ok",
                         "why", "в норме", "area", round1(area),
                         "before", inkString(ink), "after", inkString(ink), "shift", ""]);
                continue;
            }

            var shifts = [];
            var wantInk = [ink[0], ink[1], ink[2], ink[3]];
            for (var p = 0; p < plan.length; p++) {
                wantInk[INK_INDEX[plan[p].ch]] = plan[p].to;
                // Седьмое поле — прицел это или ограничитель: от него зависит,
                // как отчёт назовёт число (цель или граница нормы).
                shifts.push(plan[p].ch + ":" + round1(plan[p].from) + ":" + round1(plan[p].to)
                            + ":" + round1(plan[p].was) + ":" + plan[p].bound
                            + ":" + (plan[p].rule.aim !== undefined
                                     && plan[p].rule.aim !== null ? "1" : "0")
                            + ":" + plan[p].rule.label);
            }

            if (job.apply === false) {
                ch.remove();
                doc.activeChannels = doc.componentChannels;
                safe(function () { doc.selection.deselect(); return true; }, false);
                finding(["color", String(spec.name), "part", part, "status", "would",
                         "why", "", "area", round1(area),
                         "before", inkString(ink), "after", inkString(wantInk),
                         "shift", shifts.join("|")]);
                continue;
            }

            // Правку кладём в исходном пространстве, доводя её по замеру.
            //
            // Одна точка на кривой сдвигает СРЕДНЕЕ области заметно меньше,
            // чем саму точку: концы кривой закреплены, и пиксели по краям
            // диапазона возвращаются к исходным значениям. За один заход
            // до цели доходило меньше половины (просили M 34 -> 25, выходило
            // 31). Поэтому после каждого захода замеряем, что вышло, и тянем
            // остаток — цель при этом не пересчитывается, иначе правило,
            // считающее канал от соседнего, гоняло бы её за собой.
            var channels = [];
            for (var q = 0; q < plan.length; q++) { channels.push(plan[q].ch); }
            var after = ink;
            for (var pass = 0; pass < CORRECTION_PASSES; pass++) {
                var cur = after;
                var left = 0;
                var goal = [cur[0], cur[1], cur[2], cur[3]];
                for (var w = 0; w < channels.length; w++) {
                    var idx = INK_INDEX[channels[w]];
                    goal[idx] = wantInk[idx];
                    left = Math.max(left, Math.abs(goal[idx] - cur[idx]));
                }
                if (left < 0.7) { break; }   // ближе печать всё равно не покажет

                if (isCMYK) {
                    for (var c2 = 0; c2 < channels.length; c2++) {
                        var ci = INK_INDEX[channels[c2]];
                        applyCurve(doc, channels[c2],
                                   shiftCurve(channelFromInk(cur[ci]),
                                              channelFromInk(goal[ci])));
                    }
                } else {
                    // Для RGB-файла целевые числа возвращаются из CMYK тем же
                    // ICC: и вход, и выход считаются через один круг, чтобы
                    // неточность обратного перевода вычиталась сама.
                    var back = rgbName;
                    var rgbNow = rgbOfInk(cur, String(job.targetProfile), back, intent, bpc);
                    var rgbWant = rgbOfInk(goal, String(job.targetProfile), back, intent, bpc);
                    var nowMeans = meanChannels(doc);
                    if (nowMeans === null) { break; }
                    var keys = ["R", "G", "B"];
                    for (var r2 = 0; r2 < 3; r2++) {
                        var delta = rgbWant[r2] - rgbNow[r2];
                        if (Math.abs(delta) < 0.4) { continue; }
                        applyCurve(doc, keys[r2],
                                   shiftCurve(nowMeans.means[r2],
                                              nowMeans.means[r2] + delta));
                    }
                }
                changed = true;

                // Что вышло на самом деле — замеряем, а не считаем: кривая
                // работает по всей области, а не по её среднему.
                var st2 = meanChannels(doc);
                if (st2 === null) { break; }
                if (isCMYK) {
                    after = [inkFromChannel(st2.means[0]), inkFromChannel(st2.means[1]),
                             inkFromChannel(st2.means[2]), inkFromChannel(st2.means[3])];
                } else {
                    after = inkOfRgb([st2.means[0], st2.means[1], st2.means[2]],
                                     rgbName, String(job.targetProfile), intent, bpc);
                }
            }

            ch.remove();
            doc.activeChannels = doc.componentChannels;
            safe(function () { doc.selection.deselect(); return true; }, false);
            tmark("правка " + spec.name);
            finding(["color", String(spec.name), "part", part, "status", "corrected", "why", "",
                     "area", round1(area), "before", inkString(ink),
                     "after", inkString(after), "shift", shifts.join("|")]);
        }

        // Наши маски — тоже альфа-каналы, и уехать в файл они не должны.
        for (var z = doc.channels.length - 1; z >= 0; z--) {
            var nm = safe(function () { return String(doc.channels[z].name); }, "");
            if (nm.substr(0, 5) === "__pp_") {
                safe(function () { doc.channels[z].remove(); return true; }, false);
            }
        }
        doc.activeChannels = doc.componentChannels;

        if (was8bit) {
            safe(function () {
                doc.bitsPerChannel = BitsPerChannelType.EIGHT; return true; }, false);
        }

        if (!changed) {
            doc.close(SaveOptions.DONOTSAVECHANGES);
            doc = null;
            return done('"ok":true,"changed":false,"mode":' + jsVal(mode) + ',"error":""');
        }

        var opts = new TiffSaveOptions();
        safe(function () { opts.imageCompression = TIFFEncoding.TIFFLZW; return true; }, false);
        safe(function () { opts.embedColorProfile = true; return true; }, false);
        safe(function () { opts.layers = false; return true; }, false);
        safe(function () { opts.alphaChannels = true; return true; }, false);
        safe(function () { opts.spotColors = false; return true; }, false);
        doc.saveAs(new File(String(job.dst)), opts, true, Extension.LOWERCASE);
        tmark("запись TIFF");

        doc.close(SaveOptions.DONOTSAVECHANGES);
        doc = null;
        return done('"ok":true,"changed":true,"mode":' + jsVal(mode) + ',"error":""');
    } catch (e) {
        if (dup !== null) {
            safe(function () { dup.close(SaveOptions.DONOTSAVECHANGES); return true; }, false);
        }
        if (doc !== null) {
            safe(function () { doc.close(SaveOptions.DONOTSAVECHANGES); return true; }, false);
        }
        return done('"ok":false,"changed":false,"error":' + jsVal(errText(e)));
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
