#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTML-отчёт по прогону — один макет или сводный по нескольким.

Почему отдельный модуль, а не переделка write_markdown_report. Отчёт рисуется
ИЗ МАНИФЕСТА, а не из живых объектов прогона: манифест и есть структурированный
источник тех же данных, он уже пишется на диск и переживает прогон. Отсюда три
следствия, каждое из которых стоило бы отдельной работы при переделке markdown:

  * markdown остаётся байт в байт прежним — его читают из терминала, и рисковать
    им ради приложения незачем;
  * сводный отчёт по нескольким макетам получается тем же кодом: это просто
    список манифестов вместо одного;
  * отчёт можно перерисовать по старому прогону, ничего не пересчитывая, —
    в том числе на другом языке.

Страница самодостаточна: ни одного внешнего файла, ни одного запроса в сеть.
Приложение показывает её в WKWebView, из терминала она открывается двойным
щелчком, и её же можно отдать заказчику.

Запуск отдельно:
    python3 report_html.py вывод.html манифест.json [ещё манифесты…] [--lang en]
"""

import html
import json
import sys
from pathlib import Path

from messages import render as say

SCHEMA = 1

# --------------------------------------------------------------------------- #
#  Строки
# --------------------------------------------------------------------------- #
#
# Словарь плоский и с явными ключами: отчёт читает человек, и «почти то же
# слово» здесь заметнее, чем в коде. Английский — не машинный перевод русского,
# а препресс-термины: separation, bleed, effective ppi.
STR = {
"ru": {
    "title_one": "Препресс-отчёт", "title_many": "Препресс-отчёт: %d макетов",
    "summary": "Сводка", "run": "Прогон", "dry": "только анализ",
    "k_files": "Файлов", "k_done": "Обработано", "k_color": "Правка цвета",
    "k_warn": "Предупреждений", "k_time": "Время", "k_ok": "В норме",
    "issues": "Требует внимания", "no_issues": "Ничего, требующего внимания, не нашлось.",
    "processed": "Обработанные изображения", "will_process": "Будут обработаны",
    "memory": "Памятные цвета", "crop": "Обрезка по фрейму",
    "compliant": "Соответствуют требованиям", "skipped": "Не рассматривались",
    "lowres": "Низкое разрешение", "ai": "Прошли ИИ-апскейл — просмотреть глазами",
    "bleed": "Не хватает вылета под обрез", "vtext": "Живой текст в векторных связях",
    "spots": "Плашечные краски",
    "th_file": "Файл", "th_page": "Стр.", "th_was": "Было", "th_now": "Стало",
    "th_why": "Почему", "th_color": "Цвет", "th_part": "Область",
    "th_area": "Доля кадра", "th_ink_was": "Было CMYK", "th_ink_now": "Стало CMYK",
    "th_what": "Что сделано", "th_saved": "Сэкономлено", "th_scale": "Масштаб",
    "th_note": "Примечание", "th_ppi": "ppi", "th_reason": "Причина",
    "c_skin": "кожа", "c_sky": "небо", "c_grass": "зелень",
    "st_corrected": "поправлено", "st_ok": "в норме", "st_small": "область мала",
    "st_none": "не найдено", "st_unlike": "сюжет не подтверждён", "st_error": "сбой",
    "doc": "Документ", "saved_as": "Результат", "engine": "Движок", "chain": "Цепочка",
    "src_untouched": "исходник не изменён", "generated": "Отчёт создан",
    "more": "и ещё %d", "none": "—", "cropped": "обрезан", "nothing_to_cut": "резать нечего",
    "of": "из",
},
"en": {
    "title_one": "Prepress report", "title_many": "Prepress report: %d layouts",
    "summary": "Summary", "run": "Run", "dry": "analysis only",
    "k_files": "Files", "k_done": "Processed", "k_color": "Colour corrected",
    "k_warn": "Warnings", "k_time": "Time", "k_ok": "Compliant",
    "issues": "Needs attention", "no_issues": "Nothing needs attention.",
    "processed": "Processed images", "will_process": "Would be processed",
    "memory": "Memory colours", "crop": "Crop to frame",
    "compliant": "Already compliant", "skipped": "Not considered",
    "lowres": "Low resolution", "ai": "AI-upscaled — review by eye",
    "bleed": "Insufficient bleed", "vtext": "Live text in vector links",
    "spots": "Spot colours",
    "th_file": "File", "th_page": "Page", "th_was": "Was", "th_now": "Now",
    "th_why": "Why", "th_color": "Colour", "th_part": "Region",
    "th_area": "Frame share", "th_ink_was": "Ink before", "th_ink_now": "Ink after",
    "th_what": "Action", "th_saved": "Saved", "th_scale": "Scale",
    "th_note": "Note", "th_ppi": "ppi", "th_reason": "Reason",
    "c_skin": "skin", "c_sky": "sky", "c_grass": "greenery",
    "st_corrected": "corrected", "st_ok": "within norm", "st_small": "region too small",
    "st_none": "not found", "st_unlike": "scene not confirmed", "st_error": "failed",
    "doc": "Document", "saved_as": "Result", "engine": "Engine", "chain": "Chain",
    "src_untouched": "source untouched", "generated": "Generated",
    "more": "and %d more", "none": "—", "cropped": "cropped", "nothing_to_cut": "nothing to cut",
    "of": "of",
}}

COLOR_NAMES = {"skin": "c_skin", "sky": "c_sky", "grass": "c_grass"}
STATUS_NAMES = {"corrected": "st_corrected", "ok": "st_ok", "small": "st_small",
                "none": "st_none", "unlike": "st_unlike", "error": "st_error"}


def _t(lang, key, *args):
    s = STR.get(lang, STR["ru"]).get(key) or STR["ru"].get(key) or key
    return (s % args) if args else s


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


# --------------------------------------------------------------------------- #
#  Разбор манифеста в то, что рисуется
# --------------------------------------------------------------------------- #

def _name(p):
    return Path(str(p or "")).name


def _probe(d):
    """Блок before/after из result -> «4000x2662 CMYK»."""
    if not isinstance(d, dict):
        return ""
    w, h = d.get("width"), d.get("height")
    cs = d.get("colorspace") or ""
    if not w:
        return ""
    return "%s×%s %s" % (w, h, cs)


def _issues(m, lang):
    """
    Всё, что требует внимания, одним списком: (важность, текст, где).

    Собирается из разных разделов манифеста намеренно — человеку неважно,
    в каком именно списке скрипт держит вылеты, а в каком низкое разрешение.
    Порядок: сначала то, что нельзя отдавать в печать.
    """
    out = []
    for it in (m.get("bleed") or {}).get("issues") or []:
        edges = ", ".join("%s %.1f мм" % (e.get("edgeName"), e.get("contentBleedMm", 0))
                          for e in it.get("edges") or [])
        out.append(("crit", "%s — %s" % (_name(it.get("name") or it.get("path")),
                                         _t(lang, "bleed").lower()),
                    "%s %s · %s" % (_t(lang, "th_page"), it.get("page") or "?", edges)))
    for it in m.get("needsAiUpscale") or []:
        if it.get("aiUpscaled"):
            continue
        out.append(("crit", "%s — %.0f %s" % (_name(it.get("path")),
                                              it.get("minEffectivePpi") or 0,
                                              _t(lang, "th_ppi")),
                    it.get("skipReason") or _t(lang, "lowres")))
    for it in m.get("spotRasters") or []:
        out.append(("warn", "%s — %s" % (_name(it.get("path")), _t(lang, "spots").lower()),
                    ", ".join(it.get("spots") or [])))
    for it in m.get("vectorLinks") or []:
        if it.get("textFrames"):
            out.append(("warn", "%s — %s" % (_name(it.get("path")), _t(lang, "vtext").lower()),
                        "%s: %s" % (_t(lang, "th_page"),
                                    ", ".join(str(x) for x in it.get("pages") or []))))
    for it in m.get("processed") or []:
        for w in it.get("warnings") or []:
            out.append(("warn", "%s" % _name(it.get("path")), say(w, lang)))
    for it in m.get("skipped") or []:
        out.append(("warn", _name(it.get("name")), say(it.get("reason"), lang)))
    return out


def _counts(m):
    c = m.get("counts") or {}
    mem = sum(1 for x in (m.get("memoryColors") or []) if x.get("corrected"))
    warn = sum(len(x.get("warnings") or []) for x in (m.get("processed") or []))
    warn += len((m.get("bleed") or {}).get("issues") or [])
    warn += sum(1 for x in (m.get("needsAiUpscale") or []) if not x.get("aiUpscaled"))
    return {"files": c.get("rasterFiles") or 0, "done": c.get("processed") or 0,
            "color": mem, "warn": warn, "ok": c.get("compliant") or 0,
            "sec": m.get("elapsedSec") or 0}


# --------------------------------------------------------------------------- #
#  Куски страницы
# --------------------------------------------------------------------------- #

def _kpis(cn, lang):
    cells = [(_t(lang, "k_files"), cn["files"], ""),
             (_t(lang, "k_done"), cn["done"], "%s %d" % (_t(lang, "of"), cn["files"])),
             (_t(lang, "k_color"), cn["color"], ""),
             (_t(lang, "k_warn"), cn["warn"], ""),
             (_t(lang, "k_time"), "%.0f" % (cn["sec"] / 60.0), "мин" if lang == "ru" else "min")]
    return ('<div class="kpis">' + "".join(
        '<div class="kpi"><div class="k">%s</div><div class="v">%s<small>%s</small></div></div>'
        % (esc(k), esc(v), esc(s)) for k, v, s in cells) + "</div>")


def _issue_list(items, lang, limit=None):
    if not items:
        return '<p class="quiet">%s</p>' % esc(_t(lang, "no_issues"))
    shown = items if limit is None else items[:limit]
    rest = 0 if limit is None else max(0, len(items) - limit)
    rows = "".join(
        '<div class="issue %s"><i></i><div><b>%s</b><div class="where">%s</div></div></div>'
        % (esc(sev), esc(txt), esc(where)) for sev, txt, where in shown)
    if rest:
        rows += '<p class="quiet">%s</p>' % esc(_t(lang, "more", rest))
    return '<div class="issues">%s</div>' % rows


def _table(headers, rows, lang, mono_cols=()):
    if not rows:
        return ""
    th = "".join('<th%s>%s</th>' % (' class="mono"' if i in mono_cols else "", esc(h))
                 for i, h in enumerate(headers))
    body = ""
    for r in rows:
        tds = "".join('<td%s>%s</td>' % (' class="mono"' if i in mono_cols else "", esc(c))
                      for i, c in enumerate(r))
        body += "<tr>%s</tr>" % tds
    return ('<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
            % (th, body))


def _section(title, inner):
    if not inner:
        return ""
    return '<section><h2>%s</h2>%s</section>' % (esc(title), inner)


def _memory_rows(m, lang):
    rows = []
    for it in m.get("memoryColors") or []:
        name = _name(it.get("path"))
        pages = ", ".join(str(x) for x in it.get("pages") or [])
        for f in it.get("findings") or []:
            color = _t(lang, COLOR_NAMES.get(f.get("color"), "th_color"))
            status = _t(lang, STATUS_NAMES.get(f.get("status"), "th_what"))
            what = say(f.get("why"), lang)
            if f.get("status") == "corrected" and f.get("shift"):
                parts = []
                for chunk in str(f["shift"]).split("|"):
                    bits = chunk.split(":")
                    if len(bits) >= 6:
                        parts.append("%s %s → %s%%" % (bits[0], bits[1], bits[2]))
                what = "; ".join(parts) or status
            elif not what:
                what = status
            rows.append([name, pages, color, say(f.get("part"), lang),
                         "%.1f%%" % (f.get("area") or 0),
                         f.get("before") or "", f.get("after") or "", what])
    return rows


def _processed_rows(m, lang):
    rows = []
    for it in m.get("processed") or []:
        res = it.get("result") or {}
        rows.append([
            _name(it.get("path")),
            ", ".join(str(p.get("page")) for p in it.get("placements") or []),
            _probe(res.get("before")),
            _probe(res.get("after")),
            "; ".join(say(r, lang) for r in it.get("reasons") or []),
        ])
    return rows


def _crop_rows(m, lang):
    rows = []
    for it in m.get("cropToFrame") or []:
        if not it.get("cropped"):
            continue
        rows.append([_name(it.get("path")),
                     "%.0f%%" % (it.get("savingPercent") or 0),
                     "%.1f%% → %.0f%%" % (it.get("scaleBefore") or 0,
                                          it.get("scaleAfter") or 0),
                     say(it.get("note"), lang)])
    return rows


def _layout_panel(m, lang):
    """Подробности по одному макету."""
    cn = _counts(m)
    head = []
    head.append('<dl class="meta">')
    head.append("<dt>%s</dt><dd>%s</dd>" % (esc(_t(lang, "doc")), esc(_name(m.get("document")))))
    if m.get("documentSavedAs"):
        head.append("<dt>%s</dt><dd>%s <span class=\"quiet\">· %s</span></dd>"
                    % (esc(_t(lang, "saved_as")), esc(_name(m.get("documentSavedAs"))),
                       esc(_t(lang, "src_untouched"))))
    cfg = m.get("config") or {}
    if cfg.get("engine"):
        head.append("<dt>%s</dt><dd>%s</dd>" % (esc(_t(lang, "engine")), esc(cfg["engine"])))
    head.append("</dl>")

    parts = ["".join(head), _kpis(cn, lang), _issue_list(_issues(m, lang), lang)]
    parts.append(_section(
        _t(lang, "memory"),
        _table([_t(lang, "th_file"), _t(lang, "th_page"), _t(lang, "th_color"),
                _t(lang, "th_part"), _t(lang, "th_area"), _t(lang, "th_ink_was"),
                _t(lang, "th_ink_now"), _t(lang, "th_what")],
               _memory_rows(m, lang), lang, mono_cols=(1, 4, 5, 6))))
    parts.append(_section(
        _t(lang, "processed") if not m.get("dryRun") else _t(lang, "will_process"),
        _table([_t(lang, "th_file"), _t(lang, "th_page"), _t(lang, "th_was"),
                _t(lang, "th_now"), _t(lang, "th_why")],
               _processed_rows(m, lang), lang, mono_cols=(1, 2, 3))))
    parts.append(_section(
        _t(lang, "crop"),
        _table([_t(lang, "th_file"), _t(lang, "th_saved"), _t(lang, "th_scale"),
                _t(lang, "th_note")], _crop_rows(m, lang), lang, mono_cols=(1, 2))))
    return "".join(parts)


# --------------------------------------------------------------------------- #
#  Стиль
# --------------------------------------------------------------------------- #
#
# Тот же язык, что у приложения: системный шрифт, системный синий, светлая
# и тёмная тема через prefers-color-scheme. Цвет несёт смысл (важность
# проблемы), а не украшает.
CSS = """
:root{--bg:#F3F3F6;--panel:#FFF;--panel2:#F7F7FA;--text:#1D1D1F;--t2:#6E6E78;
--t3:#9A9AA4;--line:#DADAE1;--line2:#ECECF1;--accent:#1D6FE0;--accent-soft:#E6EFFC;
--ok:#2E9E5B;--warn:#C9820A;--warn-soft:#FBF0DA;--crit:#D7372F;--crit-soft:#FBE4E2;
color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#1C1C1F;--panel:#2A2A2F;--panel2:#303036;
--text:#F0F0F3;--t2:#9A9AA6;--t3:#6F6F7A;--line:#3A3A42;--line2:#333339;--accent:#4C93F5;
--accent-soft:#1E3050;--ok:#3FB86F;--warn:#E0A030;--warn-soft:#3A2F14;--crit:#E8534A;
--crit-soft:#3E2220;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:13.5px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",Arial,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:26px 22px 60px}
h1{font-size:21px;font-weight:700;letter-spacing:-.01em;margin:0}
.sub{color:var(--t2);margin:4px 0 20px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--t3);
font-weight:600;margin:26px 0 10px}
section{margin:0}
.mono,.num{font-variant-numeric:tabular-nums}
.mono{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12px;white-space:nowrap}
.quiet{color:var(--t3)}
dl.meta{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:0 0 18px;
background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
dl.meta dt{color:var(--t2)}dl.meta dd{margin:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:18px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.kpi .k{font-size:12px;color:var(--t2)}
.kpi .v{font-size:23px;font-weight:700;letter-spacing:-.02em;margin-top:2px;font-variant-numeric:tabular-nums}
.kpi .v small{font-size:11.5px;font-weight:500;color:var(--t3);margin-left:5px}
.issues{display:flex;flex-direction:column;gap:7px}
.issue{display:grid;grid-template-columns:4px 1fr;background:var(--panel);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
.issue i{display:block;background:var(--warn)}
.issue.crit i{background:var(--crit)}
.issue>div{padding:8px 12px}
.issue b{font-weight:600}
.issue .where{color:var(--t3);font-size:12px;margin-top:2px}
.tw{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{text-align:left;font-weight:600;color:var(--t2);font-size:11px;text-transform:uppercase;
letter-spacing:.05em;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid var(--line2);vertical-align:top}
tr:last-child td{border-bottom:0}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:0 0 16px;overflow-x:auto}
.tabs button{font:inherit;color:var(--t2);background:none;border:0;cursor:pointer;
padding:9px 13px;border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap;font-weight:500}
.tabs button[aria-selected=true]{color:var(--accent);border-bottom-color:var(--accent)}
.tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.tabs .cnt{font-size:11px;color:var(--t3);margin-left:6px;font-variant-numeric:tabular-nums}
.panel{display:none}.panel.on{display:block}
.foot{margin-top:34px;color:var(--t3);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
@media print{body{background:#fff}.tabs{display:none}.panel{display:block!important;
break-before:page}.panel:first-of-type{break-before:auto}}
"""

JS = """
document.addEventListener('click',function(e){
  var b=e.target.closest('.tabs button'); if(!b) return;
  document.querySelectorAll('.tabs button').forEach(function(x){x.setAttribute('aria-selected',x===b)});
  document.querySelectorAll('.panel').forEach(function(p){p.classList.toggle('on',p.dataset.panel===b.dataset.tab)});
});
"""


# --------------------------------------------------------------------------- #
#  Сборка
# --------------------------------------------------------------------------- #

def render(manifests, lang="ru"):
    """Список манифестов -> HTML-страница. Один манифест — обычный отчёт."""
    if isinstance(manifests, dict):
        manifests = [manifests]
    manifests = [m for m in manifests if isinstance(m, dict)]
    if not manifests:
        raise ValueError("нечего показывать: ни одного манифеста")
    many = len(manifests) > 1

    title = (_t(lang, "title_many", len(manifests)) if many
             else "%s: %s" % (_t(lang, "title_one"), _name(manifests[0].get("document"))))
    started = manifests[0].get("startedAt") or ""
    dry = any(m.get("dryRun") for m in manifests)
    sub = "%s · %s%s" % (esc(started), esc(_t(lang, "run")),
                         " · " + esc(_t(lang, "dry")) if dry else "")

    # Сводка: складываем счётчики и сводим проблемы всех макетов в один список.
    total = {"files": 0, "done": 0, "color": 0, "warn": 0, "ok": 0, "sec": 0}
    all_issues = []
    for m in manifests:
        cn = _counts(m)
        for k in total:
            total[k] += cn[k]
        pref = _name(m.get("document")) + " · " if many else ""
        all_issues += [(s, t_, pref + w) for s, t_, w in _issues(m, lang)]

    tabs = ['<button role="tab" aria-selected="true" data-tab="sum">%s</button>'
            % esc(_t(lang, "summary"))] if many else []
    panels = []
    if many:
        inner = _kpis(total, lang) + _issue_list(all_issues, lang, limit=40)
        inner += _section(_t(lang, "summary"), _table(
            [_t(lang, "doc"), _t(lang, "k_files"), _t(lang, "k_done"),
             _t(lang, "k_color"), _t(lang, "k_warn")],
            [[_name(m.get("document"))] + [_counts(m)[k] for k in ("files", "done", "color", "warn")]
             for m in manifests], lang, mono_cols=(1, 2, 3, 4)))
        panels.append('<div class="panel on" data-panel="sum">%s</div>' % inner)

    for i, m in enumerate(manifests):
        cn = _counts(m)
        if many:
            tabs.append('<button role="tab" aria-selected="false" data-tab="d%d">%s%s</button>'
                        % (i, esc(_name(m.get("document"))),
                           ('<span class="cnt">%d</span>' % cn["warn"]) if cn["warn"] else ""))
        panels.append('<div class="panel%s" data-panel="d%d">%s</div>'
                      % ("" if many else " on", i, _layout_panel(m, lang)))

    tabs_html = '<div class="tabs" role="tablist">%s</div>' % "".join(tabs) if many else ""
    return """<!doctype html>
<html lang="%s"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head>
<body><div class="wrap">
<h1>%s</h1><p class="sub">%s</p>
%s%s
<p class="foot">%s: %s · prepress.py</p>
</div><script>%s</script></body></html>""" % (
        esc(lang), esc(title), CSS, esc(title), sub, tabs_html, "".join(panels),
        esc(_t(lang, "generated")), esc(started), JS)


def write(path, manifests, lang="ru"):
    """Отрисовать и записать. Возвращает путь."""
    path = Path(path)
    path.write_text(render(manifests, lang), encoding="utf-8")
    return path


def load(paths):
    """Манифесты с диска. Битый файл пропускается с сообщением в stderr."""
    out = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError) as exc:
            print("не прочитан манифест %s: %s" % (p, exc), file=sys.stderr)
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    lang = "ru"
    if "--lang" in argv:
        i = argv.index("--lang")
        lang = argv[i + 1] if i + 1 < len(argv) else "ru"
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    out, manifests = argv[0], load(argv[1:])
    if not manifests:
        print("нечего показывать", file=sys.stderr)
        return 1
    print(write(out, manifests, lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
