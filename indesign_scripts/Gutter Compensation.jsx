/*
 * Gutter Compensation.jsx — компенсация переплёта для картинки на разворот
 * ------------------------------------------------------------------------
 * Толстый журнал в клеевом переплёте «съедает» несколько миллиметров изображения
 * в корешке. Приём: разрезать картинку по стыку разворота на две половины и
 * раздвинуть содержимое от корешка — тогда полоска у стыка печатается дважды,
 * и на сшитом развороте картинка выглядит непрерывной.
 *
 * Что делает скрипт с выделенным фреймом:
 *   1. дублирует фрейм вместе с картинкой точно на месте (дубль сверху);
 *   2. у верхнего фрейма двигает правую границу на стык разворота
 *      (верхний фрейм становится ЛЕВОЙ половиной);
 *   3. картинку внутри верхнего фрейма сдвигает ВЛЕВО на заданное смещение;
 *   4. картинку внутри исходного (нижнего) фрейма сдвигает ВПРАВО на то же смещение.
 *
 * Всё делается одним шагом отмены: Cmd+Z возвращает как было.
 *
 * Установка: положить (или сделать симлинк) в
 *   ~/Library/Preferences/Adobe InDesign/Version XX.0/en_US/Scripts/Scripts Panel/
 */

#targetengine "session"

// Значение по умолчанию в диалоге, мм.
var DEFAULT_OFFSET_MM = 5;

// --- утилиты ---------------------------------------------------------------

function safe(fn, fallback) {
    try {
        var v = fn();
        return (v === undefined) ? fallback : v;
    } catch (e) {
        return fallback;
    }
}

function isGraphicClass(name) {
    return name === "Image" || name === "PDF" || name === "EPS" ||
           name === "ImportedPage" || name === "WMF" || name === "PICT" ||
           name === "Graphic";
}

/* Из выделения достаём фрейм и графику внутри него.
   Пользователь мог выделить как сам фрейм (чёрная стрелка),
   так и картинку внутри него (белая стрелка / клик по «бублику»). */
function resolveSelection(sel) {
    var cn = safe(function () { return sel.constructor.name; }, "");

    if (isGraphicClass(cn)) {
        return { frame: sel.parent, gfx: sel };
    }
    var gfxs = safe(function () { return sel.graphics; }, null);
    if (gfxs && gfxs.length > 0) {
        return { frame: sel, gfx: gfxs[0] };
    }
    return { frame: null, gfx: null, why: cn };
}

/* Стык разворота = внутренняя граница между соседними страницами.
   Берём ту, что ближе к середине фрейма — работает и для обычного
   двухстраничного разворота, и для «острова» из трёх и более страниц. */
function findSpineX(spread, frameBounds) {
    var pages = spread.pages;
    if (pages.length < 2) { return null; }

    var mid = (frameBounds[1] + frameBounds[3]) / 2;
    var best = null, bestDist = null;

    for (var i = 0; i < pages.length; i++) {
        var b = pages[i].bounds;          // [y1, x1, y2, x2]
        var edges = [b[1], b[3]];
        for (var e = 0; e < edges.length; e++) {
            var x = edges[e];
            // внешние края разворота стыком не являются
            if (x <= frameBounds[1] || x >= frameBounds[3]) { continue; }
            var dist = Math.abs(x - mid);
            if (bestDist === null || dist < bestDist) { bestDist = dist; best = x; }
        }
    }
    return best;
}

function findSpread(frame) {
    var p = frame;
    for (var guard = 0; guard < 20 && p; guard++) {
        var cn = safe(function () { return p.constructor.name; }, "");
        if (cn === "Spread" || cn === "MasterSpread") { return p; }
        p = safe(function () { return p.parent; }, null);
    }
    return null;
}

// --- диалог ----------------------------------------------------------------

function askOffset() {
    var dlg = new Window("dialog", "Компенсация переплёта");
    dlg.orientation = "column";
    dlg.alignChildren = "fill";
    dlg.margins = 16;
    dlg.spacing = 10;

    var row = dlg.add("group");
    row.add("statictext", undefined, "Смещение картинки от корешка:");
    var fld = row.add("edittext", undefined, String(DEFAULT_OFFSET_MM));
    fld.characters = 6;
    row.add("statictext", undefined, "мм");

    var hint = dlg.add("statictext", undefined,
        "Верхняя (левая) половина уедет влево на эту величину,\n" +
        "нижняя (правая) — вправо. Смысл: вернуть на свет полоску,\n" +
        "которую съедает клеевой переплёт.\n\n" +
        "Значение = сколько съедает корешок с одной стороны,\n" +
        "обычно 3–6 мм. Из картинки выпадает полоса шириной\n" +
        "2× смещения, поэтому большие значения будут заметны.",
        { multiline: true });
    hint.characters = 54;

    var trim = dlg.add("checkbox", undefined,
        "Подрезать нижний фрейм по стыку (оставить только правую половину)");
    trim.value = false;

    var btns = dlg.add("group");
    btns.alignment = "right";
    btns.add("button", undefined, "Отмена", { name: "cancel" });
    btns.add("button", undefined, "Применить", { name: "ok" });

    if (dlg.show() !== 1) { return null; }

    var v = parseFloat(String(fld.text).replace(",", "."));
    if (isNaN(v) || v <= 0) {
        alert("Смещение должно быть положительным числом.");
        return null;
    }
    return { offset: v, trimBottom: trim.value };
}

// --- основная работа -------------------------------------------------------

/* Параметры передаём через модульные переменные: app.doScript() не отдаёт
   аргументы в функцию (они доступны только скрипту, запущенному из файла). */
var RUN_OFFSET_MM = 0;
var RUN_TRIM_BOTTOM = false;

function run() {
    var offset = Number(RUN_OFFSET_MM);
    var trimBottom = (RUN_TRIM_BOTTOM === true);

    var doc = app.activeDocument;
    var sel = app.selection[0];

    var r = resolveSelection(sel);
    if (r.frame === null) {
        alert("Выделите фрейм с картинкой.\n\nВыделено: " + (r.why || "ничего подходящего"));
        return;
    }
    var frame = r.frame;

    if (safe(function () { return frame.locked; }, false)) {
        alert("Фрейм заблокирован. Снимите блокировку и повторите.");
        return;
    }
    if (safe(function () { return frame.itemLayer.locked; }, false)) {
        alert("Слой «" + frame.itemLayer.name + "» заблокирован.");
        return;
    }

    var rot = safe(function () { return frame.absoluteRotationAngle; }, 0);
    var shear = safe(function () { return frame.absoluteShearAngle; }, 0);
    if (Math.abs(rot) > 0.001 || Math.abs(shear) > 0.001) {
        alert("Фрейм повёрнут или наклонён (поворот " + rot + "°, наклон " + shear + "°).\n" +
              "Скрипт работает только с неповёрнутыми фреймами — иначе границы\n" +
              "и сдвиги посчитаются неверно. Снимите трансформацию и повторите.");
        return;
    }

    var spread = findSpread(frame);
    if (spread === null) {
        alert("Не удалось определить разворот, на котором лежит фрейм.");
        return;
    }

    var gb = frame.geometricBounds;                  // [y1, x1, y2, x2]
    var spineX = findSpineX(spread, gb);
    if (spineX === null) {
        alert("Фрейм не пересекает стык разворота.\n\n" +
              "Границы фрейма по X: " + gb[1].toFixed(1) + " … " + gb[3].toFixed(1) + " мм.\n" +
              "Нужна картинка, лежащая сразу на двух страницах разворота.");
        return;
    }

    var gfx0 = safe(function () { return frame.graphics[0]; }, null);
    if (gfx0 === null) {
        alert("Во фрейме нет картинки.");
        return;
    }

    // Обе половины отъезжают ОТ корешка, поэтому непокрытая полоса может
    // вылезти только у стыка: у левой половины — её правый край, у правой —
    // левый. Проверяем до того, как что-то менять.
    var ib = gfx0.geometricBounds;                    // [y1, x1, y2, x2]
    var gaps = [];
    var gL = Math.max(0, spineX - (ib[3] - offset));  // левая половина, край у стыка
    var gR = Math.max(0, (ib[1] + offset) - spineX);  // правая половина, край у стыка
    if (gL > 0.1) { gaps.push("у левой половины со стороны корешка: " + gL.toFixed(1) + " мм"); }
    if (gR > 0.1) { gaps.push("у правой половины со стороны корешка: " + gR.toFixed(1) + " мм"); }
    if (gaps.length > 0) {
        var go = confirm(
            "Картинке не хватает запаса на смещение " + offset + " мм.\n" +
            "После сдвига появится непокрытая область:\n  • " + gaps.join("\n  • ") + "\n\n" +
            "Уменьшите смещение или возьмите исходник побольше.\n\nВсё равно продолжить?",
            false, "Компенсация переплёта");
        if (!go) { return; }
    }

    // Автоподгонка при изменении размера фрейма перемасштабировала бы картинку —
    // на время работы выключаем и потом возвращаем как было.
    var autoFitWas = safe(function () { return frame.frameFittingOptions.autoFit; }, null);
    if (autoFitWas === true) { frame.frameFittingOptions.autoFit = false; }

    // 1. Дубль точно на месте, поверх исходника.
    var dup = frame.duplicate();
    dup.geometricBounds = gb;

    var dupFitWas = safe(function () { return dup.frameFittingOptions.autoFit; }, null);
    if (dupFitWas === true) { dup.frameFittingOptions.autoFit = false; }

    // 2. Правую границу верхнего фрейма — на стык.
    dup.geometricBounds = [gb[0], gb[1], gb[2], spineX];

    // 3. Картинку в верхнем (левом) фрейме — влево.
    var dupGfx = safe(function () { return dup.graphics[0]; }, null);
    if (dupGfx === null) {
        alert("В дубле не оказалось картинки — операция прервана, отмените через Cmd+Z.");
        return;
    }
    dupGfx.move(undefined, [-offset, 0]);

    // 4. Картинку в исходном (правом) фрейме — вправо.
    gfx0.move(undefined, [offset, 0]);

    // Необязательно: обрезать нижний фрейм по стыку, чтобы под левой
    // половиной не лежала лишняя копия картинки.
    if (trimBottom) {
        frame.geometricBounds = [gb[0], spineX, gb[2], gb[3]];
    }

    if (autoFitWas === true) { frame.frameFittingOptions.autoFit = true; }
    if (dupFitWas === true) { dup.frameFittingOptions.autoFit = true; }

    app.selection = [dup, frame];
}

// --- точка входа -----------------------------------------------------------

(function () {
    if (app.documents.length === 0) {
        alert("Нет открытого документа.");
        return;
    }
    if (app.selection.length !== 1) {
        alert("Выделите ровно один фрейм с картинкой.\n\nСейчас выделено: " +
              app.selection.length);
        return;
    }

    var params = askOffset();
    if (params === null) { return; }

    var doc = app.activeDocument;
    var vp = doc.viewPreferences;

    // Считаем и двигаем в миллиметрах от левого верхнего угла разворота,
    // независимо от того, как настроены линейки у пользователя.
    var oldH = vp.horizontalMeasurementUnits;
    var oldV = vp.verticalMeasurementUnits;
    var oldOrigin = vp.rulerOrigin;
    var oldZero = doc.zeroPoint;

    try {
        vp.horizontalMeasurementUnits = MeasurementUnits.MILLIMETERS;
        vp.verticalMeasurementUnits = MeasurementUnits.MILLIMETERS;
        vp.rulerOrigin = RulerOrigin.SPREAD_ORIGIN;
        doc.zeroPoint = [0, 0];

        RUN_OFFSET_MM = params.offset;
        RUN_TRIM_BOTTOM = params.trimBottom;

        // ENTIRE_SCRIPT — весь скрипт откатывается одним Cmd+Z.
        app.doScript(run, ScriptLanguage.JAVASCRIPT, [],
                     UndoModes.ENTIRE_SCRIPT, "Компенсация переплёта");
    } finally {
        vp.horizontalMeasurementUnits = oldH;
        vp.verticalMeasurementUnits = oldV;
        vp.rulerOrigin = oldOrigin;
        doc.zeroPoint = oldZero;
    }
})();
