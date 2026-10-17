/*
 * Prepress Automation.jsx
 * -----------------------
 * Обёртка для панели Scripts в InDesign: запускает prepress.py для активного
 * документа.
 *
 * Важно: python запускается ОТСОЕДИНЁННО (nohup ... &). Иначе получился бы
 * дедлок — этот скрипт занимал бы движок InDesign, а prepress.py в это время
 * пытался бы вызвать в том же InDesign relink.jsx и ждал бы вечно.
 * Поэтому скрипт стартует обработку и сразу завершается; ход работы виден
 * в лог-файле, а по окончании прилетит системное уведомление macOS.
 *
 * Установка:
 *   положить (или сделать симлинк) в
 *   ~/Library/Preferences/Adobe InDesign/Version XX.0/en_US/Scripts/Scripts Panel/
 */

#targetengine "session"

(function () {
    // --- где лежит prepress.py ---------------------------------------------
    // Скрипт ожидает, что рядом с папкой jsx/ лежит prepress.py. В панели Scripts
    // файл обычно стоит симлинком, поэтому путь при необходимости
    // доразрешаем через readlink.
    function shq(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }
    function asq(s) {
        return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
    }
    function sh(cmd) {
        return String(app.doScript("do shell script " + asq(cmd),
                                   ScriptLanguage.APPLESCRIPT_LANGUAGE));
    }

    function findPy(selfPath) {
        var candidates = [];
        var f = new File(selfPath);
        candidates.push(f.parent.parent.fsName + "/prepress.py");
        try {
            // readlink -f разворачивает симлинк до настоящего файла
            var real = sh("/usr/bin/readlink -f " + shq(f.fsName) + " || true");
            if (real) {
                candidates.push(new File(real).parent.parent.fsName + "/prepress.py");
            }
        } catch (e) { /* readlink недоступен — идём дальше по кандидатам */ }
        for (var i = 0; i < candidates.length; i++) {
            var p = new File(candidates[i]);
            if (p.exists) { return p; }
        }
        return null;
    }

    var py = findPy($.fileName);
    if (py === null) {
        alert("Не найден prepress.py рядом с этим скриптом.\n\n" +
              "Ожидается структура:\n" +
              "  prepress_automation/prepress.py\n" +
              "  prepress_automation/jsx/Prepress Automation.jsx\n\n" +
              "Этот файл: " + $.fileName);
        return;
    }

    if (app.documents.length === 0) {
        alert("Нет открытого документа.");
        return;
    }

    var doc = app.activeDocument;
    var docFile = null;
    try { docFile = doc.fullName; } catch (e) { docFile = null; }
    if (!docFile || !doc.saved) {
        alert("Документ ещё ни разу не сохранён.\nСохраните его на диск и запустите скрипт снова.");
        return;
    }
    if (doc.modified) {
        var go = confirm("В документе есть несохранённые изменения.\n" +
                         "Скрипт в конце выполнит Save — они будут записаны в файл.\n\n" +
                         "Продолжить?");
        if (!go) { return; }
    }

    // --- режим --------------------------------------------------------------
    var dlg = new Window("dialog", "Препресс: обработка изображений");
    dlg.orientation = "column";
    dlg.alignChildren = "left";
    dlg.margins = 16;

    var info = dlg.add("statictext", undefined,
        "Документ: " + doc.name, { multiline: false });
    info.characters = 50;

    var rbFull = dlg.add("radiobutton", undefined, "Обработать (CMYK, даунсэмпл, TIFF, relink)");
    var rbDry = dlg.add("radiobutton", undefined, "Только анализ — ничего не менять (dry-run)");
    rbFull.value = true;

    var btns = dlg.add("group");
    btns.alignment = "right";
    btns.add("button", undefined, "Отмена", { name: "cancel" });
    btns.add("button", undefined, "Запустить", { name: "ok" });

    if (dlg.show() !== 1) { return; }
    var dryRun = rbDry.value;

    // --- запуск -------------------------------------------------------------
    var bootLog = "/tmp/prepress_boot_" + (new Date().getTime()) + ".log";
    var cmd = "nohup /usr/bin/python3 " + shq(py.fsName) +
              (dryRun ? " --dry-run" : "") + " " + shq(docFile.fsName) +
              " > " + shq(bootLog) + " 2>&1 & echo $!";

    var pid = "";
    try {
        pid = sh(cmd);
    } catch (e) {
        alert("Не удалось запустить prepress.py:\n" + e);
        return;
    }

    var logDir = docFile.parent.fsName + "/prepress_logs";
    alert("Обработка запущена" + (dryRun ? " (только анализ)" : "") + ".\n\n" +
          "PID: " + pid + "\n" +
          "Логи: " + logDir + "\n\n" +
          "InDesign во время работы скрипта будет ненадолго переключаться —\n" +
          "не трогайте документ до системного уведомления об окончании.\n\n" +
          "Стартовый вывод (на случай ошибок): " + bootLog);
})();
