#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Постраничный экспорт вёрстки InDesign в PDF.

Каждая полоса уходит в отдельный файл «<трёхзначный номер>_<код выпуска>.pdf»
(038_Y123.pdf). Код выпуска берётся из имени .indd — оно всегда начинается
с него. Готовые PDF складываются в папку PressPDF на уровень выше папки
макета, туда, где собирается весь номер.

    export_pdf.py --dry-run путь/к/макету.indd     # только показать, что выйдет
    export_pdf.py путь/к/макету.indd               # экспорт
    export_pdf.py путь/к/папке                     # все *_CMYK.indd внутри

Вся работа с InDesign — в jsx/presspdf_export.jsx; здесь цикл, журнал и CLI.
Цикл по полосам ведётся отсюда, а не внутри JSX, чтобы индикатор в дроплете
показывал ход работы: экспорт полосы — секунды, а номер бывает и в сорок полос.
"""

import argparse
import datetime as _dt
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSX_PATH = HERE / "jsx" / "presspdf_export.jsx"

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "prepress_automation"
PARAMS_PATH = APP_SUPPORT / "presspdf_params.json"
LOG_PATH = Path.home() / "Library" / "Logs" / "presspdf.log"

CONFIG_PATH = HERE / "config.json"
# Запасной вариант на случай, если config.json потерялся: без него экспорт
# просто не с чем запускать, а падать из-за конфига обиднее, чем взять
# заведомо рабочее имя.
FALLBACK_PRESET = "[Press Quality]"
INDESIGN_APP_ID = "com.adobe.InDesign"


def configured_preset():
    """Пресет из config.json — там же, где и остальные настройки проекта."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        name = str((cfg.get("pdf_export") or {}).get("preset") or "").strip()
        if name:
            return name
    except (OSError, ValueError):
        pass
    return FALLBACK_PRESET

# В папке берём только готовые к печати макеты: обычный .indd рядом — это
# исходник со связями на RGB-картинки, из него печатный PDF собирать нельзя.
CMYK_SUFFIX = "_CMYK"

# osascript и так ждёт ответа InDesign, но у Apple Events свой таймаут в 120 с.
# Экспорт одной полосы в печатном качестве занимает секунды, открытие тяжёлой
# вёрстки — десятки; час с запасом.
JSX_TIMEOUT_SEC = 3600


# --------------------------------------------------------------------------- #
#  Журнал
# --------------------------------------------------------------------------- #
#
# Пишем и в stdout, и в файл. stdout читает дроплет (он же строит по нему
# индикатор), файл остаётся после того, как окно закрылось.

_log_file = None


def _open_log():
    global _log_file
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _log_file = open(LOG_PATH, "a", encoding="utf-8")
    except OSError:
        _log_file = None      # без журнала работать можно, без экспорта — нет


def say(msg=""):
    print(msg, flush=True)
    if _log_file is not None:
        _log_file.write("%s  %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
        _log_file.flush()


def log_stage(what):
    """
    Что делается прямо сейчас — строка для индикатора в Dock.

    Формат тот же, что у prepress.py: дроплет вылавливает её регуляркой
    «этап +: .*». Пишется ПЕРЕД началом операции, иначе индикатор молчит
    ровно то время, которое операция и занимает.
    """
    say("        этап        : %s" % what)


# --------------------------------------------------------------------------- #
#  Вызов JSX
# --------------------------------------------------------------------------- #


def _as_quote(s):
    """Строковый литерал AppleScript."""
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def run_jsx(action, workdir, **params):
    """
    Одно действие внутри InDesign. Возвращает разобранный отчёт.

    Параметры уходят файлом, а не через «do script with arguments»: после
    прерванного прогона движок скриптов InDesign отдаёт следующим вызовам
    аргументы предыдущего. В параметрах лежит одноразовый nonce, который
    скрипт возвращает; не совпал — останавливаемся, потому что дальше мы
    работали бы по чужим данным.
    """
    nonce = uuid.uuid4().hex
    report = Path(workdir) / ("report_%s.json" % nonce[:8])

    params.update(action=action, nonce=nonce, outPath=str(report))
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    with open(PARAMS_PATH, "w", encoding="utf-8") as fh:
        json.dump(params, fh, ensure_ascii=True)

    script = (
        "with timeout of {t} seconds\n"
        "  tell application id {appid}\n"
        "    do script (POSIX file {jsx}) language javascript\n"
        "  end tell\n"
        "end timeout\n"
    ).format(t=JSX_TIMEOUT_SEC, appid=_as_quote(INDESIGN_APP_ID),
             jsx=_as_quote(str(JSX_PATH)))

    proc = subprocess.run(["osascript", "-"], input=script,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("InDesign не выполнил «%s»: %s"
                           % (action, proc.stderr.strip() or "без сообщения"))

    if not report.exists():
        raise RuntimeError("Скрипт InDesign не создал отчёт для «%s»." % action)
    with open(report, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if data.get("nonce") != nonce:
        raise RuntimeError(
            "Скрипт InDesign отработал со СТАРЫМИ параметрами.\n"
            "Обычно это значит, что предыдущий прогон был прерван и движок "
            "скриптов InDesign завис. Перезапустите InDesign и повторите.")
    if not data.get("ok"):
        detail = data.get("errorDetail")
        if detail and detail != data.get("error") and _log_file is not None:
            _log_file.write("            подробно: %s\n" % detail)
            _log_file.flush()
        raise RuntimeError(data.get("error") or "скрипт не сказал, что не так")
    return data


# --------------------------------------------------------------------------- #
#  Что экспортировать
# --------------------------------------------------------------------------- #


def is_layout(path):
    name = path.name
    # Файлы восстановления InDesign и служебные точки-файлы макетами не являются.
    return (path.suffix.lower() == ".indd"
            and not name.startswith(".")
            and not name.startswith("~"))


def collect_documents(paths, quiet=False):
    """
    Из брошенного набора собираем список макетов.

    Файл берём какой дали: раз человек показал пальцем, он знает, что делает.
    В папке — только *_CMYK.indd: рядом с ними лежат исходники со связями
    на RGB-картинки, и печатный PDF из такого файла был бы браком.

    quiet — для --list: дроплету нужны только пути, по одному на строку,
    чтобы показать человеку, что именно он бросил. Правило одно и живёт
    здесь; повторять его ещё и в AppleScript значило бы завести второе,
    которое рано или поздно разойдётся с этим.
    """
    found, seen = [], set()

    def note(msg):
        if not quiet:
            say(msg)

    def add(p):
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            found.append(rp)

    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            if is_layout(p):
                add(p)
            else:
                note("!! %s — это не .indd, пропущен" % p.name)
        elif p.is_dir():
            inside = [f for f in sorted(p.rglob("*.indd"))
                      if is_layout(f) and f.stem.lower().endswith(CMYK_SUFFIX.lower())]
            if not inside:
                note("!! в папке %s нет ни одного файла %s.indd" % (p.name, CMYK_SUFFIX))
            for f in inside:
                add(f)
        else:
            note("!! не найден: %s" % p)

    return found


# --------------------------------------------------------------------------- #
#  Экспорт одного макета
# --------------------------------------------------------------------------- #


def export_document(doc_path, preset, workdir, dry_run=False, among=""):
    """Возвращает (сколько полос вышло, сколько всего, список ошибок)."""
    log_stage("разбор документа")
    plan = run_jsx("plan", workdir, docPath=str(doc_path), preset=preset)

    pages = plan.get("pages") or []
    out_dir = plan.get("outDir") or ""
    say("    код выпуска : %s" % plan.get("code"))
    say("    полос       : %d" % len(pages))
    say("    папка PDF   : %s" % out_dir)
    for w in plan.get("warnings") or []:
        say("    !! %s" % w)

    if dry_run:
        for pg in pages:
            say("       стр. %-6s -> %s" % (pg.get("label"), pg.get("fileName")))
        # Документ, который открыли мы, за собой и закрываем.
        run_jsx("close", workdir, docPath=str(doc_path),
                weOpenedIt=bool(plan.get("weOpenedIt")))
        return 0, len(pages), []

    done, errors, sent = 0, [], []
    try:
        for i, pg in enumerate(pages, 1):
            # Шаги индикатора в дроплете — это ПОЛОСЫ, а не макеты: номер
            # часто лежит одним файлом, и по макетам бегунок простоял бы
            # в нуле весь прогон, сообщая «1 из 1». Формат строки разбирает
            # runInBackground в PressPDF.applescript — менять его нельзя.
            say("[%d/%d] %s" % (i, len(pages), doc_path.name))
            log_stage("стр. %s -> %s%s" % (pg.get("label"), pg.get("fileName"), among))
            try:
                run_jsx("page", workdir, docPath=str(doc_path), preset=preset,
                        pageIndex=pg.get("index"), filePath=pg.get("filePath"))
                done += 1
                sent.append(pg)
            except Exception as exc:
                # Одна упавшая полоса не повод бросать остальные тридцать девять.
                errors.append("%s — %s" % (pg.get("fileName"), exc))
                say("    !! %s — %s" % (pg.get("fileName"), exc))
    finally:
        log_stage("закрытие документа")
        try:
            run_jsx("close", workdir, docPath=str(doc_path),
                    weOpenedIt=bool(plan.get("weOpenedIt")))
        except Exception as exc:
            say("    !! документ не закрылся: %s" % exc)

    flatplan_send(plan, sent)
    return done, len(pages), errors


def flatplan_send(plan, pages):
    """
    Отдаёт готовые PDF помощнику флэтплана — он в фоне рисует превью, делает
    префлайт и отправляет (flatplan/tools/flatplan_pdf.py). Помощник кладёт
    на Mac «Подключить InDesign к флэтплану»; нет его — ничего не делаем.
    """
    helper = Path.home() / "Library/Application Support/Flatplan/flatplan_pdf.py"
    if not pages or not helper.exists():
        return
    task = Path(tempfile.gettempdir()) / ("flatplan_task_%d.json" % int(time.time() * 1000))
    task.write_text(json.dumps({
        "kind": "print", "docName": plan.get("docName") or "", "binding": plan.get("flatplan") or "",
        "pageCount": plan.get("pageCount") or len(pages),
        "items": [{"file": p.get("filePath"), "indexes": [p.get("index")], "pageNames": [p.get("label")]}
                  for p in pages],
    }, ensure_ascii=False), encoding="utf-8")
    subprocess.Popen(["/usr/bin/python3", str(helper), str(task)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    say("    флэтплан: %d PDF отправляются в фоне" % len(pages))


# --------------------------------------------------------------------------- #
#  Запись настройки
# --------------------------------------------------------------------------- #


def set_preset(name):
    """
    Запоминает пресет в config.json.

    Записью занимается prepress.py: config.json пишется с сохранением
    комментариев и порядка ключей, и заводить здесь второй такой писатель
    значило бы обречь его однажды разойтись с первым. Это единственное место,
    где export_pdf.py зависит от prepress.py, и зависимость нестрогая —
    без него настройку просто не сохранить, экспорт работает по-прежнему.
    """
    name = str(name).strip()
    if not name:
        print("Имя пресета пустое — нечего запоминать.", flush=True)
        return 2
    try:
        sys.path.insert(0, str(HERE))
        import prepress
    except Exception as exc:
        print("Не удалось сохранить: рядом нет рабочего prepress.py (%s)" % exc,
              flush=True)
        return 2
    try:
        shown = prepress.config_set(str(CONFIG_PATH), "pdf_export.preset=%s" % name)
    except ValueError as exc:
        print("Не сохранено: %s" % exc, flush=True)
        return 2
    print(shown, flush=True)
    return 0


# --------------------------------------------------------------------------- #
#  Уведомление
# --------------------------------------------------------------------------- #


def notify(title, text):
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title %s'
             % (_as_quote(text), _as_quote(title))],
            capture_output=True, text=True, timeout=15)
    except Exception:
        pass      # уведомление — приятное дополнение, а не часть работы


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #


def build_parser():
    p = argparse.ArgumentParser(
        description="Постраничный экспорт вёрстки InDesign в PDF "
                    "(имена вида 038_Y123.pdf, папка PressPDF на уровень выше).")
    # nargs="*" — потому что --list-presets и --set-preset путей не требуют.
    p.add_argument("paths", nargs="*", metavar="ПУТЬ",
                   help="файлы .indd или папки; в папке берутся только *%s.indd"
                        % CMYK_SUFFIX)
    p.add_argument("--preset", default=None,
                   help="пресет экспорта PDF; по умолчанию pdf_export.preset "
                        "из config.json (сейчас «%s»)" % configured_preset())
    p.add_argument("--dry-run", action="store_true",
                   help="только показать, какие файлы получились бы")
    p.add_argument("--list", action="store_true",
                   help="напечатать пути найденных макетов по одному на строку "
                        "и выйти (этим пользуется PressPDF.app)")
    p.add_argument("--list-presets", action="store_true",
                   help="напечатать пресеты экспорта PDF, установленные "
                        "в InDesign; первой строкой — выбранный сейчас")
    p.add_argument("--set-preset", metavar="ИМЯ",
                   help="запомнить пресет в config.json (pdf_export.preset)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.preset:
        args.preset = configured_preset()

    if args.list_presets:
        # Первой строкой — выбранный сейчас, чтобы окно выбора могло его подсветить.
        print(configured_preset(), flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix="presspdf_") as workdir:
                data = run_jsx("presets", workdir)
        except Exception as exc:
            # InDesign может быть не запущен или занят. Это не повод обрывать
            # настройку: выбранный пресет уже напечатан, а окно, получив пустой
            # список, предложит ввести имя руками.
            print("не удалось спросить InDesign: %s" % exc, file=sys.stderr)
            return 0
        for name in data.get("presets") or []:
            print(name, flush=True)
        return 0

    if args.set_preset is not None:
        return set_preset(args.set_preset)

    if not args.paths:
        build_parser().error("не указано, что экспортировать")

    if args.list:
        # Ни журнала, ни шапки: вывод разбирает дроплет.
        docs = collect_documents(args.paths, quiet=True)
        for d in docs:
            print(d, flush=True)
        return 0 if docs else 1

    _open_log()

    started = _dt.datetime.now()
    say("")
    say("=" * 78)
    say("Экспорт PDF · %s · пресет «%s»%s"
        % (started.strftime("%Y-%m-%d %H:%M:%S"), args.preset,
           "  [ТОЛЬКО ПРОСМОТР]" if args.dry_run else ""))

    if not JSX_PATH.exists():
        say("Не найден %s" % JSX_PATH)
        return 2

    docs = collect_documents(args.paths)
    if not docs:
        say("Нечего экспортировать.")
        notify("Экспорт PDF", "Не нашёл ни одного макета")
        return 1

    say("Макетов: %d" % len(docs))
    say("=" * 78)

    total_done, total_pages, failed_docs = 0, 0, []
    with tempfile.TemporaryDirectory(prefix="presspdf_") as workdir:
        for n, doc in enumerate(docs, 1):
            say("")
            say("--- макет %d из %d: %s ---" % (n, len(docs), doc.name))
            among = ""
            if len(docs) > 1:
                among = " · макет %d из %d" % (n, len(docs))
            try:
                done, pages, errors = export_document(doc, args.preset, workdir,
                                                      dry_run=args.dry_run,
                                                      among=among)
                total_done += done
                total_pages += pages
                if errors:
                    failed_docs.append("%s: %d из %d" % (doc.name, len(errors), pages))
            except Exception as exc:
                say("    !! %s" % exc)
                failed_docs.append("%s: %s" % (doc.name, exc))

    elapsed = (_dt.datetime.now() - started).total_seconds()
    say("")
    say("=" * 78)
    tail = ""
    if failed_docs:
        # Иначе «6 из 6 полос» соседствует со списком ошибок и читается так,
        # будто всё прошло гладко: полосы упавшего макета в знаменатель
        # не попали, потому что их не удалось даже сосчитать.
        tail = " · макетов с ошибками: %d из %d" % (len(failed_docs), len(docs))
    if args.dry_run:
        say("ИТОГ за %.1f с: полос к экспорту %d в %d макетах%s"
            % (elapsed, total_pages, len(docs), tail))
    else:
        say("ИТОГ за %.1f с: экспортировано %d из %d полос в %d макетах%s"
            % (elapsed, total_done, total_pages, len(docs), tail))
    for f in failed_docs:
        say("  !! %s" % f)
    say("=" * 78)

    if not args.dry_run:
        if failed_docs:
            notify("Экспорт PDF", "Готово %d из %d полос, с ошибками"
                   % (total_done, total_pages))
        else:
            notify("Экспорт PDF", "Готово: %d полос" % total_done)

    return 1 if failed_docs else 0


if __name__ == "__main__":
    sys.exit(main())
