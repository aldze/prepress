/*
 * presspdf_export.jsx
 * -------------------
 * Экспорт вёрстки в PDF постранично: каждая полоса — отдельный файл с именем
 * «<трёхзначный номер полосы>_<код выпуска>.pdf», например 038_Y123.pdf.
 * Код выпуска берётся из имени .indd (оно всегда начинается с него):
 * y123_p036-039_Name_CMYK.indd -> Y123.
 *
 * Готовые PDF складываются в папку PressPDF НА УРОВЕНЬ ВЫШЕ папки макета —
 * туда, где лежат папки всех полос выпуска, так что весь номер собирается
 * в одном месте:
 *
 *     y123 Folder/
 *     ├── y123_p036-039 Folder/y123_p036-039_Test_CMYK.indd
 *     ├── y123_p040-041 Folder/…
 *     └── PressPDF/038_Y123.pdf, 039_Y123.pdf, …
 *
 * Два режима запуска:
 *
 *   1. Из панели Scripts InDesign — файла параметров нет. Работает по
 *      активному документу и показывает итог окном.
 *
 *   2. Из export_pdf.py (а значит и из PressPDF.app) — параметры лежат
 *      в файле, ответ пишется в JSON. Работа разбита на три действия
 *      (plan / page / close), чтобы питон вёл цикл по полосам сам и мог
 *      показывать ход работы: экспорт одной полосы — секунды, а номера
 *      бывают по сорок полос.
 *
 * Параметры (JSON):
 *   {"action":"plan"|"page"|"close", "nonce":"...", "outPath":"/…/report.json",
 *    "docPath":"/…/y123_p036-039_Test_CMYK.indd", "preset":"[Press Quality]",
 *    "pageIndex":0, "filePath":"/…/PressPDF/036_Y123.pdf"}
 */

/* Параметры берём файлом, а не из arguments: прерванный прогон оставляет
   движок скриптов InDesign в состоянии, когда он отдаёт следующим вызовам
   аргументы предыдущего. Тот же приём и та же причина, что в collect_links.jsx.
   Здесь у файла есть и вторая роль: по его отсутствию скрипт понимает, что
   запущен из панели Scripts, и переходит в разговорный режим. Поэтому файл
   удаляется сразу после чтения — иначе следующий запуск из панели молча ушёл
   бы в пакетный режим со старыми параметрами. */
var PARAMS_PATH = Folder.userData.fsName + "/prepress_automation/presspdf_params.json";

/* Запасной вариант: пресет живёт в config.json (pdf_export.preset), и в
   пакетном режиме его присылает export_pdf.py. Это имя берётся, только если
   конфиг не нашёлся — падать из-за него обиднее, чем взять заведомо рабочее. */
var FALLBACK_PRESET = "[Press Quality]";
var OUT_DIR_NAME = "PressPDF";


// --- служебное -------------------------------------------------------------

function safe(fn, fallback) {
    try {
        var v = fn();
        if (v === undefined) { return fallback; }
        return v;
    } catch (e) {
        return fallback;
    }
}

// Всё, что вне ASCII, экранируется в \uXXXX: файл получается чистым ASCII,
// поэтому кириллица в именах не зависит от кодировки при чтении в Python.
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
    if (v instanceof Array) {
        var parts = [];
        for (var i = 0; i < v.length; i++) { parts.push(jsVal(v[i])); }
        return "[" + parts.join(",") + "]";
    }
    return '"' + jsEsc(v) + '"';
}

// Пары складываются массивом ["ключ", значение, ...], чтобы порядок полей
// в JSON был предсказуемым.
function jsObj(pairs) {
    var parts = [];
    for (var i = 0; i < pairs.length; i += 2) {
        parts.push('"' + jsEsc(pairs[i]) + '":' + jsVal(pairs[i + 1]));
    }
    return "{" + parts.join(",") + "}";
}

function readFileText(p) {
    var f = new File(p);
    if (!f.exists) { return null; }
    f.encoding = "UTF-8";
    f.open("r");
    var t = f.read();
    f.close();
    return t;
}

function writeFileText(p, txt) {
    var f = new File(p);
    f.encoding = "UTF-8";
    f.lineFeed = "Unix";
    f.open("w");
    f.write(txt);
    f.close();
}

function readParams() {
    var txt = readFileText(PARAMS_PATH);
    if (!txt) { return null; }
    // Съесть файл сразу: он одноразовый, см. комментарий у PARAMS_PATH.
    safe(function () { return new File(PARAMS_PATH).remove(); }, false);
    // В ExtendScript нет JSON.parse — читаем JSON-литерал через eval.
    return eval("(" + txt + ")");
}


// --- настройки проекта -----------------------------------------------------

function shq(v) { return "'" + String(v).replace(/'/g, "'\\''") + "'"; }
function asq(v) {
    return '"' + String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
}

/* config.json лежит рядом с папкой jsx/. В панели Scripts этот файл стоит
   симлинком, поэтому $.fileName показывает на симлинк, а не на репозиторий —
   тогда доразрешаем путь через readlink, как это делает Prepress Automation.jsx. */
function projectConfigPath() {
    var f = new File($.fileName);
    var direct = safe(function () { return f.parent.parent.fsName + "/config.json"; }, "");
    if (direct && new File(direct).exists) { return direct; }
    var real = safe(function () {
        return String(app.doScript("do shell script " +
            asq("/usr/bin/readlink -f " + shq(f.fsName) + " || true"),
            ScriptLanguage.APPLESCRIPT_LANGUAGE));
    }, "");
    if (real) {
        var viaLink = safe(function () {
            return new File(real).parent.parent.fsName + "/config.json";
        }, "");
        if (viaLink && new File(viaLink).exists) { return viaLink; }
    }
    return "";
}

// Пресет — из общих настроек проекта: панель настроек Prepress пишет его туда же.
function configuredPreset() {
    var p = projectConfigPath();
    if (!p) { return FALLBACK_PRESET; }
    var txt = readFileText(p);
    if (!txt) { return FALLBACK_PRESET; }
    // Ключи-комментарии в config.json начинаются с подчёркивания, так что
    // сам файл остаётся обычным JSON и читается литералом.
    var cfg = safe(function () { return eval("(" + txt + ")"); }, null);
    var name = safe(function () { return String(cfg.pdf_export.preset); }, "");
    if (name) { return name; }
    return FALLBACK_PRESET;
}


// --- документ --------------------------------------------------------------

// /tmp и /private/tmp — один и тот же каталог; InDesign отдаёт то один вариант,
// то другой, поэтому пути сравниваем в канонической форме.
function canonPath(p) {
    p = String(p || "");
    if (p.indexOf("/private/") === 0) { p = p.substring(8); }
    return p;
}

// doc.fullName бросает «Unsaved documents have no full name», если в документе
// есть несохранённые изменения. doc.filePath работает всегда.
function docFullPath(d) {
    var p = safe(function () { return d.fullName.fsName; }, "");
    if (!p) { p = safe(function () { return d.filePath.fsName + "/" + d.name; }, ""); }
    return p;
}

function findOpenDocument(docPath) {
    var want = canonPath(new File(docPath).fsName);
    for (var i = 0; i < app.documents.length; i++) {
        if (canonPath(docFullPath(app.documents[i])) === want) { return app.documents[i]; }
    }
    return null;
}

/* Открывает документ, если он ещё не открыт. Возвращает {doc, opened}: opened
   говорит, наш ли это документ и можно ли его закрывать. Чужой документ,
   который человек держит открытым, закрывать нельзя — там могут быть правки.
   Открываем БЕЗ окна: так экспорт номера заметно быстрее и не мельтешит
   на экране. */
function openDocument(docPath) {
    var d = findOpenDocument(docPath);
    if (d !== null) { return {doc: d, opened: false}; }
    var f = new File(docPath);
    if (!f.exists) { throw new Error("Файл не найден: " + docPath); }
    return {doc: app.open(f, false), opened: true};
}


// --- имена -----------------------------------------------------------------

/* Код выпуска — с начала имени файла: «y123_p036-039_Name_CMYK.indd» -> «Y123».
   Строчная y приводится к прописной, как в готовых PDF. */
function issueCode(baseName) {
    var m = String(baseName).match(/^[yY](\d+)/);
    if (!m) { return null; }
    return "Y" + m[1];
}

function baseNameOf(p) {
    var s = String(p);
    var i = s.lastIndexOf("/");
    if (i >= 0) { s = s.substring(i + 1); }
    var j = s.lastIndexOf(".");
    if (j > 0) { s = s.substring(0, j); }
    return s;
}

function pad3(s) {
    s = String(s);
    while (s.length < 3) { s = "0" + s; }
    return s;
}

/* Папка для готовых PDF — на уровень выше папки макета: там собирается
   весь номер. Если выше уровня нет (макет лежит у корня диска), кладём рядом
   с документом — это лучше, чем упасть. */
function outputDirFor(docPath) {
    var f = new File(docPath);
    var up = safe(function () { return f.parent.parent; }, null);
    if (up === null) { up = safe(function () { return f.parent; }, null); }
    if (up === null) { throw new Error("Не удалось определить папку для PDF: " + docPath); }
    return up.fsName + "/" + OUT_DIR_NAME;
}

function ensureFolder(path) {
    var d = new Folder(path);
    if (!d.exists && !d.create()) {
        throw new Error("Не удалось создать папку: " + path);
    }
    return d;
}


// --- разбор документа ------------------------------------------------------

/* Что экспортировать и подо что. Возвращает список полос с готовыми именами
   файлов плюс замечания, которые стоит показать человеку. */
function planDocument(doc, docPath) {
    var warnings = [];
    var base = baseNameOf(docPath);
    var code = issueCode(base);
    if (code === null) {
        throw new Error("Имя файла «" + base + "» не начинается с кода выпуска " +
                        "(ожидается y и число, например y123). " +
                        "Без него не собрать имя PDF.");
    }

    var outDir = outputDirFor(docPath);
    var pages = [];
    var used = {};

    for (var i = 0; i < doc.pages.length; i++) {
        var pg = doc.pages[i];
        var label = String(safe(function () { return pg.name; }, ""));
        // В имени файла нужен только номер. Нумерация бывает и не цифровой
        // (римская, с префиксом секции «A-1») — тогда берём порядковый номер
        // полосы в документе и говорим об этом вслух: молча выдать не тот
        // номер полосы хуже, чем предупредить.
        var digits = label.replace(/[^0-9]/g, "");
        if (digits === "") {
            digits = String(i + 1);
            warnings.push("page \"" + label + "\" is not numbered with digits — " +
                          "взят порядковый номер " + digits);
        }

        var name = pad3(digits) + "_" + code;
        if (used[name]) {
            // Две полосы с одинаковым номером — обычно сбитая нумерация секций.
            // Второй файл получает суффикс, иначе первый молча пропал бы.
            used[name] = used[name] + 1;
            warnings.push("page number " + pad3(digits) + " is not unique — " +
                          "файл сохранён как " + name + "_" + used[name] + ".pdf");
            name = name + "_" + used[name];
        } else {
            used[name] = 1;
        }

        pages.push({
            index: i,
            label: label,
            fileName: name + ".pdf",
            filePath: outDir + "/" + name + ".pdf"
        });
    }

    // Битая связь в печатном PDF — это брак тиража, а не мелочь: при
    // NEVER_INTERACT InDesign не спросит о ней, поэтому спрашиваем сами.
    var missing = 0, outOfDate = 0;
    var links = safe(function () { return doc.links; }, null);
    if (links) {
        for (var k = 0; k < links.length; k++) {
            var st = safe(function () { return links[k].status; }, null);
            if (st === LinkStatus.LINK_MISSING) { missing++; }
            else if (st === LinkStatus.LINK_OUT_OF_DATE) { outOfDate++; }
        }
    }
    if (missing > 0) { warnings.push("missing links: " + missing); }
    if (outOfDate > 0) { warnings.push("out-of-date links: " + outOfDate); }

    return {code: code, outDir: outDir, pages: pages, warnings: warnings};
}


// --- экспорт ---------------------------------------------------------------

function resolvePreset(name) {
    var preset = app.pdfExportPresets.itemByName(name);
    if (!safe(function () { return preset.isValid; }, false)) {
        throw new Error("Пресет экспорта «" + name + "» не найден в InDesign.");
    }
    /* Пресет с включёнными разворотами сделал бы задачу невыполнимой: в файл
       одной полосы попал бы весь разворот, и имя «038_Y123.pdf» врало бы.
       Молча выключить настройку пресета нельзя — человек мог поставить её
       намеренно, — поэтому останавливаемся и говорим, что править. */
    if (safe(function () { return preset.exportReaderSpreads; }, false)) {
        throw new Error("В пресете «" + name + "» включён экспорт разворотами " +
                        "(Spreads). Постранично с ним не получится — " +
                        "выключите Spreads в настройках пресета.");
    }
    return preset;
}

/* Одна полоса — один файл. Адресуем полосу через «+N», порядковым номером
   в документе, а не подписью: подписи в разных секциях повторяются, и
   диапазон «36» мог бы указать не на ту полосу. */
function exportPage(doc, preset, pageIndex, filePath) {
    app.pdfExportPreferences.viewPDF = false;
    app.pdfExportPreferences.pageRange = "+" + (pageIndex + 1);
    doc.exportFile(ExportFormat.PDF_TYPE, new File(filePath), false, preset);
}

// Диапазон живёт в настройках приложения, а не документа: оставить его
// суженным — значит подсунуть человеку «страница 12» в его следующем
// экспорте руками.
function restorePageRange() {
    safe(function () {
        app.pdfExportPreferences.pageRange = PageRange.ALL_PAGES;
        return true;
    }, false);
}


// --- флэтплан -----------------------------------------------------------------
/* Отправка готовых PDF во флэтплан. Этот блок одинаковый в PressPDF Export
   и Tablet PDF Export (исходник — flatplan/indesign/flatplan_hook.jsxinc).

   После экспорта отдаёт список PDF помощнику флэтплана
   (~/Library/Application Support/Flatplan/flatplan_pdf.py). Тот в фоне рисует
   превью, делает префлайт и отправляет; об итоге — уведомление macOS. InDesign
   не ждёт. Помощника нет (этот Mac не подключён к флэтплану) — блок ничего
   не делает, экспорт работает как раньше.

   items: [{file: путь к PDF, indexes: [номера полос в документе с нуля],
            pageNames: [подписи полос], wide: разворот целиком}] */

function fpJson(v) {
    if (v === null || v === undefined) { return "null"; }
    if (typeof v === "number") { return isFinite(v) ? String(v) : "null"; }
    if (typeof v === "boolean") { return v ? "true" : "false"; }
    if (v instanceof Array) {
        var a = [];
        for (var i = 0; i < v.length; i++) { a.push(fpJson(v[i])); }
        return "[" + a.join(",") + "]";
    }
    if (typeof v === "object") {
        var o = [];
        for (var k in v) { if (v.hasOwnProperty(k)) { o.push(fpJson(String(k)) + ":" + fpJson(v[k])); } }
        return "{" + o.join(",") + "}";
    }
    var s = String(v), out = "";
    for (var j = 0; j < s.length; j++) {
        var c = s.charCodeAt(j);
        if (c === 34) { out += '\\"'; }
        else if (c === 92) { out += "\\\\"; }
        else if (c < 32 || c > 126) { var h = c.toString(16); while (h.length < 4) { h = "0" + h; } out += "\\u" + h; }
        else { out += s.charAt(j); }
    }
    return '"' + out + '"';
}

/** Возвращает строку для итогового окна или "" (Mac не подключён, отправлять нечего). */
function fpSend(doc, kind, items) {
    try {
        var helper = new File(Folder.userData.fsName + "/Flatplan/flatplan_pdf.py");
        if (!helper.exists || !items.length) { return ""; }
        var binding = "";
        try { binding = String(doc.extractLabel("flatplan") || ""); } catch (e1) { binding = ""; }
        var task = new File(Folder.temp.fsName + "/flatplan_task_" + new Date().getTime() + ".json");
        task.encoding = "UTF-8";
        task.open("w");
        task.write(fpJson({kind: kind, docName: String(doc.name), binding: binding,
                           pageCount: doc.pages.length, items: items}));
        task.close();
        var q = function (s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; };
        var cmd = "/usr/bin/nohup /usr/bin/python3 " + q(helper.fsName) + " " + q(task.fsName) +
                  " >/dev/null 2>&1 &";
        app.doScript("do shell script \"" + cmd.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + "\"",
                     ScriptLanguage.APPLESCRIPT_LANGUAGE);
        return "Флэтплан: " + items.length + " PDF отправляются в фоне — о результате придёт уведомление.";
    } catch (e) {
        return "Флэтплан: отправить не удалось — " + (e.message || e);
    }
}
// --- /флэтплан ----------------------------------------------------------------


// --- режим панели Scripts --------------------------------------------------

function runInteractive() {
    if (app.documents.length === 0) {
        alert("Нет открытого документа.\n\nОткройте вёрстку и запустите скрипт снова.");
        return;
    }
    var doc = app.activeDocument;
    var docPath = docFullPath(doc);
    if (!docPath) {
        alert("Документ ещё ни разу не сохранён.\n\n" +
              "Имя PDF собирается из имени файла, так что сначала сохраните вёрстку.");
        return;
    }

    /* Окна показываем только после того, как вернули userInteractionLevel:
       при NEVER_INTERACT alert() подавляется, и сообщение об ошибке просто
       не дошло бы до человека. Поэтому вся работа — внутри try, а разговор —
       строго после finally. */
    var prev = app.scriptPreferences.userInteractionLevel;
    app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;
    var plan = null, done = 0, failed = [], fatal = "", fpItems = [];
    try {
        var preset = resolvePreset(configuredPreset());
        plan = planDocument(doc, docPath);
        ensureFolder(plan.outDir);
        for (var i = 0; i < plan.pages.length; i++) {
            var pg = plan.pages[i];
            try {
                exportPage(doc, preset, pg.index, pg.filePath);
                done++;
                fpItems.push({file: pg.filePath, indexes: [pg.index], pageNames: [pg.label]});
            } catch (e) {
                failed.push(pg.fileName + ": " + e);
            }
        }
    } catch (err) {
        fatal = String(err);
    } finally {
        restorePageRange();
        app.scriptPreferences.userInteractionLevel = prev;
    }

    if (fatal !== "") {
        alert("Экспорт не выполнен.\n\n" + fatal);
        return;
    }

    var msg = "Готово: " + done + " из " + plan.pages.length + " полос.\n\n" +
              "Папка: " + plan.outDir;
    if (plan.warnings.length > 0) {
        msg += "\n\nОбратите внимание:\n• " + plan.warnings.join("\n• ");
    }
    if (failed.length > 0) {
        msg += "\n\nНе получилось:\n• " + failed.join("\n• ");
    }
    var fpMsg = fpSend(doc, "print", fpItems);
    if (fpMsg) { msg += "\n\n" + fpMsg; }
    alert(msg);
}


// --- пакетный режим --------------------------------------------------------

function pagesToJson(pages) {
    var parts = [];
    for (var i = 0; i < pages.length; i++) {
        parts.push(jsObj([
            "index", pages[i].index,
            "label", pages[i].label,
            "fileName", pages[i].fileName,
            "filePath", pages[i].filePath
        ]));
    }
    return "[" + parts.join(",") + "]";
}

function runBatch(p) {
    var action = String(p.action || "plan");
    var outPath = String(p.outPath || "");
    if (!outPath) { throw new Error("В параметрах нет outPath."); }

    var head = ['"schema":1', '"nonce":' + jsVal(String(p.nonce || "")),
                '"action":' + jsVal(action)];

    var prev = app.scriptPreferences.userInteractionLevel;
    app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;
    try {
        if (action === "plan") {
            var preset = resolvePreset(String(p.preset || FALLBACK_PRESET));
            var opened = openDocument(String(p.docPath));
            /* Если разбор не удался, документ закрываем ЗДЕСЬ. Питон в этом
               случае до своего close не доходит — исключение прилетает ему
               раньше, — и документ остался бы висеть открытым. На папке
               с полусотней макетов, у которых, скажем, не разобрать код
               выпуска, InDesign молча набрал бы полсотни открытых файлов. */
            var plan;
            try {
                plan = planDocument(opened.doc, String(p.docPath));
                ensureFolder(plan.outDir);
            } catch (planErr) {
                if (opened.opened) {
                    safe(function () { opened.doc.close(SaveOptions.NO); return true; }, false);
                }
                throw planErr;
            }
            head.push('"weOpenedIt":' + jsVal(opened.opened));
            head.push('"code":' + jsVal(plan.code));
            head.push('"outDir":' + jsVal(plan.outDir));
            head.push('"warnings":' + jsVal(plan.warnings));
            head.push('"pages":' + pagesToJson(plan.pages));
            // флэтплану: к какому материалу привязан документ (см. export_pdf.py)
            head.push('"flatplan":' + jsVal(safe(function () { return String(opened.doc.extractLabel("flatplan") || ""); }, "")));
            head.push('"docName":' + jsVal(String(opened.doc.name)));
            head.push('"pageCount":' + jsVal(opened.doc.pages.length));

        } else if (action === "page") {
            var preset2 = resolvePreset(String(p.preset || FALLBACK_PRESET));
            var d = findOpenDocument(String(p.docPath));
            if (d === null) {
                throw new Error("Документ уже закрыт: " + p.docPath);
            }
            exportPage(d, preset2, Number(p.pageIndex), String(p.filePath));
            head.push('"filePath":' + jsVal(String(p.filePath)));

        } else if (action === "presets") {
            // Список установленных пресетов — для выбора в настройках
            // PressPDF.app: имя пресета должно совпадать с InDesign посимвольно,
            // и давать его вводить руками значит напрашиваться на опечатку.
            var names = [];
            for (var i = 0; i < app.pdfExportPresets.length; i++) {
                var nm = safe(function () { return app.pdfExportPresets[i].name; }, "");
                if (nm) { names.push(nm); }
            }
            head.push('"presets":' + jsVal(names));

        } else if (action === "close") {
            var d2 = findOpenDocument(String(p.docPath));
            if (d2 !== null && p.weOpenedIt === true) {
                // Экспорт документ не меняет, но сам факт открытия InDesign
                // иногда помечает как правку — сохранять нечего и незачем.
                d2.close(SaveOptions.NO);
                head.push('"closed":true');
            } else {
                head.push('"closed":false');
            }
            restorePageRange();

        } else {
            throw new Error("Неизвестное действие: " + action);
        }

        head.push('"ok":true');
        head.push('"error":' + jsVal(""));
    } catch (err) {
        restorePageRange();
        head.push('"ok":false');
        // error — то, что можно показать человеку; errorDetail — то же плюс
        // строка скрипта, нужная только при разборе поломки. Смешивать их
        // в одной строке значит показывать «(line 229)» верстальщику.
        head.push('"error":' + jsVal(String(err.message || err)));
        head.push('"errorDetail":' + jsVal(String(err) +
                  (err.line ? (" (line " + err.line + ")") : "")));
    } finally {
        app.scriptPreferences.userInteractionLevel = prev;
    }

    writeFileText(outPath, "{" + head.join(",") + "}");
    return "OK";
}


// --- вход ------------------------------------------------------------------

function main() {
    var p = readParams();
    if (p === null || !p.outPath) {
        // Нет параметров — значит запуск из панели Scripts.
        runInteractive();
        return "OK";
    }
    return runBatch(p);
}

try {
    main();
} catch (err) {
    // Сюда попадают только сбои вне runBatch (например, нечитаемые параметры):
    // ошибки самой работы уже разложены по полю error в отчёте.
    var ef = new File(PARAMS_PATH + ".error");
    ef.encoding = "UTF-8";
    ef.open("w");
    ef.write(String(err) + (err.line ? (" (line " + err.line + ")") : ""));
    ef.close();
    throw err;
}
