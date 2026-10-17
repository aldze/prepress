#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepress.py — предпечатная подготовка изображений, связанных с документом InDesign.

Что делает:
  1. Через AppleScript просит InDesign открыть .indd и выгрузить данные обо всех
     связях (collect_links.jsx): путь, цветовое пространство, actual/effective PPI,
     масштаб размещения, страница.
  2. Через ImageMagick замеряет каждый файл на диске (пиксели, ppi, ICC-профиль).
  3. Решает, какие изображения не соответствуют требованиям:
       - не CMYK (RGB / Lab);
       - CMYK, но профиль не целевой;
       - effective PPI вне рабочего диапазона.
  4. Обрабатывает несоответствующие: ресемплинг до ровно 300 effective PPI
     (вниз с 350+, вверх с 200-290), цветоделение в целевой ICC, шарпинг,
     запись в TIFF 8 бит LZW. Ниже 200 ppi не трогает — выносит в отчёт.
  5. Оригиналы переносит в папку originals рядом с .indd.
  6. Переподключает связи на новые TIFF (relink.jsx) и сохраняет документ.
  7. Пишет подробный лог, JSON-манифест и markdown-отчёт в prepress_logs.

Запуск:
    ./prepress.py "/path/to/doc.indd"
    ./prepress.py --active                 # активный документ InDesign
    ./prepress.py --dry-run "/path/doc.indd"

Требования: macOS, Adobe InDesign, ImageMagick 7 (brew install imagemagick).
"""

import argparse
import datetime as _dt
import json
import logging
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

# Памятные цвета без Adobe живут отдельным файлом: это единственный кусок,
# у которого есть второй, полностью независимый движок.
import native_color
from messages import msg, render as say, render_all as say_all

HERE = Path(__file__).resolve().parent
JSX_DIR = HERE / "jsx"
DEFAULT_CONFIG = HERE / "config.json"

log = logging.getLogger("prepress")


# --------------------------------------------------------------------------- #
#  Конфигурация
# --------------------------------------------------------------------------- #

def resolve_magick(cfg):
    """
    Ищет ImageMagick. Из панели Scripts и из дроплета скрипт запускается с
    урезанным PATH (/usr/bin:/bin:/usr/sbin:/sbin), поэтому одного `which` мало —
    проверяем и типовые места установки Homebrew.
    """
    candidates = []
    configured = cfg.get("magick")
    if configured:
        candidates.append(os.path.expanduser(configured))
    candidates += ["/opt/homebrew/bin/magick", "/usr/local/bin/magick"]
    for c in candidates:
        p = Path(c)
        if p.is_absolute() and p.exists() and os.access(str(p), os.X_OK):
            return str(p)
    found = shutil.which(configured or "magick") or shutil.which("magick")
    return found


# Где macOS держит ICC-профили. Используется как запасной вариант поиска,
# если путь из конфига на этой машине не существует.
COLORSYNC_DIRS = [
    Path("/Library/ColorSync/Profiles"),
    Path("/System/Library/ColorSync/Profiles"),
    Path.home() / "Library" / "ColorSync" / "Profiles",
]


def resolve_icc(cfg_value):
    """
    Приводит путь к ICC-профилю к абсолютному.

    Порядок: ~ -> относительный от папки проекта -> поиск по имени файла
    в системных папках ColorSync. Последний шаг нужен, чтобы конфиг,
    написанный на одной машине, не ломался на другой.
    """
    if not cfg_value:
        return None
    p = Path(os.path.expanduser(str(cfg_value)))
    if not p.is_absolute():
        p = HERE / p
    if p.exists():
        return p
    for d in COLORSYNC_DIRS:
        cand = d / Path(cfg_value).name
        if cand.exists():
            return cand
    return None


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    # ключи-комментарии в рабочих данных не нужны
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- #
#  Мост в InDesign (AppleScript -> do script ... language javascript)
# --------------------------------------------------------------------------- #

def _as_quote(s):
    """Экранирование строки для литерала AppleScript."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# Параметры для скриптов InDesign передаются файлом, а не через
# `do script ... with arguments`. Причина: если прогон прервать (Ctrl+C, зависшее
# модальное окно), движок скриптов InDesign застревает и начинает отдавать
# следующим вызовам аргументы предыдущего — до перезапуска приложения. Файл от
# этого не зависит, а nonce позволяет поймать чтение устаревших параметров.
JSX_PARAMS_PATH = (Path.home() / "Library" / "Application Support" /
                   "prepress_automation" / "jsx_params.json")


def write_jsx_params(payload, path=JSX_PARAMS_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    nonce = "%s-%d" % (_dt.datetime.now().strftime("%Y%m%d%H%M%S%f"), os.getpid())
    payload["nonce"] = nonce
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=True)
    return nonce


def run_jsx(cfg, jsx_path, args, purpose=""):
    """Выполняет .jsx внутри InDesign и возвращает stdout osascript."""
    jsx_path = Path(jsx_path).resolve()
    if not jsx_path.exists():
        raise FileNotFoundError("InDesign script not found: %s" % jsx_path)

    # arguments оставлены как запасной путь для ручного запуска скрипта.
    arg_list = ", ".join(_as_quote(a) for a in args)
    script = (
        'with timeout of {timeout} seconds\n'
        '  tell application id {appid}\n'
        '    do script (POSIX file {jsx}) language javascript with arguments {{{args}}}\n'
        '  end tell\n'
        'end timeout\n'
    ).format(
        timeout=int(cfg.get("applescript_timeout_sec", 3600)),
        appid=_as_quote(cfg.get("indesign_app_id", "com.adobe.InDesign")),
        jsx=_as_quote(str(jsx_path)),
        args=arg_list,
    )

    log.debug("AppleScript (%s):\n%s", purpose or jsx_path.name, script)
    proc = subprocess.run(
        ["osascript", "-"], input=script,
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "InDesign did not run %s.\nosascript stderr: %s"
            % (jsx_path.name, proc.stderr.strip())
        )
    return proc.stdout.strip()


def _read_jsx_json(out_path):
    err_path = Path(str(out_path) + ".error")
    if err_path.exists():
        msg = err_path.read_text(encoding="utf-8", errors="replace")
        err_path.unlink()
        raise RuntimeError("Error inside the InDesign script: %s" % msg)
    if not Path(out_path).exists():
        raise RuntimeError("The InDesign script wrote no result file: %s" % out_path)
    with open(out_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _check_nonce(data, nonce, what):
    got = data.get("nonce")
    if got != nonce:
        raise RuntimeError(
            "The InDesign script (%s) ran with the PREVIOUS parameters "
            "(expected nonce %s, got %s).\n"
            "Usually this means an earlier run was interrupted and the "
            "InDesign script engine is stuck. Restart InDesign and retry."
            % (what, nonce, got))


def collect_links(cfg, doc_path, workdir):
    out_path = Path(workdir) / "links.json"
    if out_path.exists():
        out_path.unlink()
    nonce = write_jsx_params({"docPath": doc_path or "-", "outPath": str(out_path)})
    run_jsx(cfg, JSX_DIR / "collect_links.jsx",
            [doc_path or "-", str(out_path)], purpose="collecting links")
    data = _read_jsx_json(out_path)
    _check_nonce(data, nonce, "collecting links")
    return data


def clean_pasteboard(cfg, doc_path, workdir, apply=True):
    """Убирает картинки с полей документа. Возвращает отчёт JSX."""
    out_path = Path(workdir) / "pasteboard.json"
    if out_path.exists():
        out_path.unlink()
    nonce = write_jsx_params({"docPath": doc_path or "-", "outPath": str(out_path),
                              "apply": bool(apply)})
    run_jsx(cfg, JSX_DIR / "clean_pasteboard.jsx",
            [doc_path or "-", str(out_path)], purpose="cleaning the pasteboard")
    data = _read_jsx_json(out_path)
    _check_nonce(data, nonce, "cleaning the pasteboard")
    return data


def relink_document(cfg, doc_path, mapping, workdir, save=True, save_as=None,
                    was_modified=False):
    map_path = Path(workdir) / "relink_map.json"
    rep_path = Path(workdir) / "relink_report.json"
    for p in (map_path, rep_path):
        if p.exists():
            p.unlink()
    # was_modified замеряет collect_links.jsx до того, как тронет линейки:
    # к моменту relink документ помечен изменённым в любом случае. Значение
    # нужно только когда переподключать нечего — см. relink.jsx.
    payload = {"save": save, "map": mapping, "docWasModified": bool(was_modified)}
    if save_as:
        payload["saveAs"] = str(save_as)
    with open(map_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=True)
    nonce = write_jsx_params({"docPath": doc_path or "-", "mapPath": str(map_path),
                              "outPath": str(rep_path)})
    run_jsx(cfg, JSX_DIR / "relink.jsx",
            [doc_path or "-", str(map_path), str(rep_path)], purpose="relinking")
    data = _read_jsx_json(rep_path)
    _check_nonce(data, nonce, "relinking")
    return data


# --------------------------------------------------------------------------- #
#  ImageMagick
# --------------------------------------------------------------------------- #

_PROBE_FIELDS = [
    ("width", "%w"), ("height", "%h"), ("colorspace", "%[colorspace]"),
    ("xres", "%x"), ("yres", "%y"), ("units", "%[units]"),
    ("depth", "%z"), ("channels", "%[channels]"), ("type", "%[type]"),
    ("profiles", "%[profiles]"), ("icc_desc", "%[icc:description]"),
    ("format", "%[magick]"), ("filesize", "%B"), ("scenes", "%n"),
]
_PROBE_FMT = "\x1f".join(v for _, v in _PROBE_FIELDS)
_SEP = "\x1f"


def probe_image(cfg, path):
    """magick identify -> словарь свойств первого кадра файла."""
    # -ping: читать только заголовок, не декодируя пиксели (быстрее в ~30 раз).
    # [0] — только первый кадр: у многослойного PSD/TIFF иначе будет N строк.
    proc = subprocess.run(
        [cfg["magick"], "identify", "-ping", "-quiet",
         "-format", _PROBE_FMT, "%s[0]" % str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("magick identify: %s" % proc.stderr.strip())

    parts = proc.stdout.split(_SEP)
    keys = [k for k, _ in _PROBE_FIELDS]
    info = dict(zip(keys, parts + [""] * (len(keys) - len(parts))))

    for k in ("width", "height", "depth"):
        info[k] = int(float(info[k])) if info[k].strip() else 0
    for k in ("xres", "yres"):
        try:
            info[k] = float(info[k])
        except ValueError:
            info[k] = 0.0
    try:
        info["filesize"] = int(info["filesize"])
    except ValueError:
        info["filesize"] = Path(path).stat().st_size

    # ppi независимо от того, в каких единицах записана плотность
    factor = 2.54 if "centimeter" in info.get("units", "").lower() else 1.0
    info["ppi_x"] = info["xres"] * factor
    info["ppi_y"] = info["yres"] * factor

    info["profile_list"] = [p.strip() for p in info.get("profiles", "").split(",") if p.strip()]
    info["has_icc"] = "icc" in info["profile_list"]
    info["cs"] = normalize_colorspace(info.get("colorspace", ""))
    # "%[channels]" даёт что-то вроде "srgb 3.0" / "srgba 4.0" / "cmyk 4.0"
    chan = (info.get("channels") or "").split()
    info["has_alpha"] = bool(chan) and chan[0].lower().endswith("a")
    # Число каналов: по нему видно лишние — альфу и плашечные. У CMYK их
    # должно быть ровно четыре, и пятый означает, что в файле есть что-то,
    # чего в печать так просто не отдашь.
    try:
        info["channel_count"] = int(float(chan[1])) if len(chan) > 1 else 0
    except (ValueError, IndexError):
        info["channel_count"] = 0
    return info


def normalize_colorspace(cs):
    c = (cs or "").strip().lower()
    if c.startswith("cmyk"):
        return "CMYK"
    if c.startswith("gray"):
        return "GRAY"
    if c == "lab":
        return "LAB"
    return "RGB"


COLOR_DIRECTIONS = ("to_cmyk", "to_rgb")


def color_direction(cfg):
    """
    Куда переводим цвет: "to_cmyk" (обычный препресс) или "to_rgb" (обратно).

    Обратный перевод нужен, когда готовый печатный материал переиздаётся
    на экране: сайт, планшетная версия, презентация. Отдельный ключ, а не
    угадывание по исходнику, потому что в одной пачке бывают и RGB, и CMYK,
    и «сделай как надо» тут значит разное.
    """
    value = str(cfg.get("color_direction") or "to_cmyk").strip().lower()
    return value if value in COLOR_DIRECTIONS else "to_cmyk"


def target_rgb_icc(cfg):
    """Целевой RGB-профиль для обратного перевода."""
    return cfg.get("target_rgb_icc") or cfg.get("assume_rgb_icc")


def target_icc_for(cfg, cs):
    """
    Целевой профиль цветоделения для данного исходного пространства.

    Серые уходят в отдельный профиль (grayscale_icc, по умолчанию GCR70):
    высокий GCR держит нейтраль преимущественно в K, поэтому серое фото
    в печати не разъезжается по четырём краскам. Всё остальное — в target_icc.
    """
    if cs == "GRAY" and cfg.get("grayscale_icc"):
        return cfg["grayscale_icc"]
    return cfg["target_icc"]


def assumed_src_icc(cfg, cs):
    """Каким профилем считать исходник, если своего ICC в файле нет."""
    if cs == "CMYK":
        return cfg.get("assume_cmyk_icc") or cfg["target_icc"]
    if cs == "GRAY":
        return cfg.get("assume_gray_icc") or cfg["assume_rgb_icc"]
    return cfg["assume_rgb_icc"]


def icc_description(cfg, icc_path):
    """Название профиля внутри .icc — чтобы сравнивать с профилем изображения."""
    proc = subprocess.run(
        [cfg["magick"], "-size", "1x1", "xc:white", "-profile", str(icc_path),
         "-format", "%[icc:description]", "info:"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


# --------------------------------------------------------------------------- #
#  Шарпинг — ЗАГЛУШКА
# --------------------------------------------------------------------------- #

def sharpen_args(cfg, ctx):
    """
    ЗАГЛУШКА под будущий шарпинг.

    Сейчас по умолчанию возвращает пустой список — резкость не наводится.
    Чтобы включить: в config.json выставить sharpen.enabled = true; тогда
    в команду magick добавится -unsharp с параметрами из sharpen.unsharp.

    ctx — словарь с данными файла (resize_factor, cs, final_w/h и т.п.),
    на случай если позже понадобится считать радиус от степени уменьшения.
    """
    if not stage_on(cfg, "sharpen"):
        # Этап выключен цепочкой. Возвращаем пустые аргументы,
        # а не None: вызывающая сторона пишет note в план прогона.
        return [], "skipped (stage not in the chain)"
    sh = cfg.get("sharpen") or {}
    if not sh.get("enabled"):
        return [], "skipped (stub disabled)"
    params = sh.get("unsharp", "0x1.0+1.0+0.05")
    return ["-unsharp", params], "unsharp %s" % params


# --------------------------------------------------------------------------- #
#  ИИ-апскейл (Real-ESRGAN)
# --------------------------------------------------------------------------- #

def ai_cfg(cfg):
    return cfg.get("ai_upscale") or {}


def ai_paths(cfg):
    """Абсолютные пути к бинарнику и моделям (в конфиге они от папки проекта)."""
    a = ai_cfg(cfg)
    binary = Path(os.path.expanduser(a.get("binary") or ""))
    models = Path(os.path.expanduser(a.get("model_dir") or ""))
    if not binary.is_absolute():
        binary = HERE / binary
    if not models.is_absolute():
        models = HERE / models
    return binary, models


def ai_available(cfg):
    if not stage_on(cfg, "upscale"):
        return False, "the stage is not in the chain"
    if not ai_cfg(cfg).get("enabled"):
        return False, "disabled in config.json"
    binary, models = ai_paths(cfg)
    if not binary.exists():
        return False, ("the engine is not installed — run %s"
                       % (HERE / "tools" / "install_realesrgan.sh"))
    if not os.access(str(binary), os.X_OK):
        return False, "the binary is not executable: %s" % binary
    if not models.is_dir():
        return False, "model folder not found: %s" % models
    return True, ""


def ai_reject_reason(cand, cfg):
    """Почему конкретный файл нельзя прогнать через Real-ESRGAN (или None)."""
    info = cand.probe
    if info["cs"] == "CMYK":
        return ("the source is already CMYK — an RGB model would ruin the colour; "
                "upscale before separation")
    if info["cs"] == "GRAY":
        return "the source is greyscale — the model works in RGB"
    mpx = (info["width"] * info["height"]) / 1e6
    limit = float(ai_cfg(cfg).get("max_input_mpx") or 0)
    if limit and mpx > limit:
        return "the source is %.1f Mpx, over the %.1f Mpx limit" % (mpx, limit)
    return None


def run_ai_upscale(cfg, src_path, workdir, tag):
    """
    Прогоняет файл через Real-ESRGAN и возвращает (путь_к_png, список_шагов).

    Движок не умеет ICC: он читает пиксели как есть и пишет PNG без профиля.
    Поэтому исходник сначала приводится к sRGB (с учётом встроенного профиля),
    и только потом идёт в модель — иначе Adobe RGB молча уехал бы в sRGB-числа.
    """
    a = ai_cfg(cfg)
    binary, models = ai_paths(cfg)
    steps = []

    # 1. Нормализация в sRGB
    srgb_in = Path(workdir) / ("ai_%s_in.png" % tag)
    pre = [cfg["magick"], "%s[0]" % str(src_path)]
    probe = probe_image(cfg, src_path)
    if not probe["has_icc"]:
        pre += ["-profile", str(cfg["assume_rgb_icc"])]
        steps.append('AI upscale: source profile assumed "%s"'
                     % Path(cfg["assume_rgb_icc"]).name)
    else:
        pre += ["-intent", cfg["rendering_intent"], "-profile", str(cfg["assume_rgb_icc"])]
        steps.append('AI upscale: "%s" -> sRGB before the model (it has no ICC)'
                     % probe.get("icc_desc"))
    pre += ["-depth", "8", str(srgb_in)]
    proc = subprocess.run(pre, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("preparing for AI upscale: %s" % proc.stderr.strip())

    # 2. Собственно апскейл
    scale = int(a.get("scale") or 4)
    ai_out = Path(workdir) / ("ai_%s_out.png" % tag)
    cmd = [str(binary), "-i", str(srgb_in), "-o", str(ai_out),
           "-n", str(a.get("model") or "realesrgan-x4plus"),
           "-m", str(models), "-s", str(scale)]
    steps.append("AI upscale: %s x%d (%s)"
                 % (a.get("model"), scale, binary.name))
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=float(a.get("timeout_sec") or 900))
    if proc.returncode != 0 or not ai_out.exists():
        raise RuntimeError("Real-ESRGAN returned code %d: %s"
                           % (proc.returncode, (proc.stderr or "").strip()[-400:]))

    after = probe_image(cfg, ai_out)
    steps.append("AI upscale: %dx%d -> %dx%d px"
                 % (probe["width"], probe["height"], after["width"], after["height"]))
    return ai_out, steps, cmd


# --------------------------------------------------------------------------- #
#  Принятие решения по каждому файлу
# --------------------------------------------------------------------------- #

class Candidate(object):
    """Один файл на диске и все его размещения в макете."""

    def __init__(self, path):
        self.path = Path(path)
        # Строки путей ровно в том виде, в каком их отдал InDesign. Один и тот же
        # файл он может назвать по-разному (/tmp vs /private/tmp), а relink ищет
        # связи по точному совпадению — поэтому храним все варианты.
        self.link_paths = []
        self.placements = []          # записи из collect_links.jsx
        self.probe = None
        self.reasons = []
        self.warnings = []
        self.needs_ai_upscale = None  # effective PPI, если ниже upscale_min_ppi
        self.ai_skip_reason = None    # почему ИИ-апскейл не применён
        self.ai_done = False
        self.skip_reason = None
        self.spot_info = None         # плашечные каналы в файле (TIFF/PSD)
        self.needs_spot_merge = False
        self.spot_source = None       # временный TIFF после сведения плашек
        self.spot_merged = []
        self.color_source = None      # временный TIFF после цветоделения в Photoshop
        self.color_by = None          # чем поделен цвет: "magick" | "photoshop"
        self.correct_source = None    # временный TIFF после правки памятных цветов
        self.master_path = None       # заготовка для планшетной версии
        self.crop = None              # план обрезки по фрейму (plan_crop)
        self.crop_source = None       # временный файл после обрезки
        self.crop_note = None         # почему обрезка невозможна
        self.pre_source = None        # временный TIFF после отдельного ресемплинга
        self.skin_distrust_noted = False   # предупреждение о маске уже записано
        self.resample_done = False    # ресемплинг сделан отдельным вызовом magick
        self.color_findings = []      # что нашлось по памятным цветам
        self.color_corrected = False  # правка действительно применена
        self.needs_color = False
        self.needs_resample = False
        self.resample_kind = None     # "downsample" | "upsample"
        self.resize_factor = 1.0
        self.out_path = None
        self.result = None

    @property
    def min_effective_ppi(self):
        vals = []
        for p in self.placements:
            for k in ("effectivePpiH", "effectivePpiV"):
                v = p.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    vals.append(float(v))
        return min(vals) if vals else None

    @property
    def max_effective_ppi(self):
        vals = []
        for p in self.placements:
            for k in ("effectivePpiH", "effectivePpiV"):
                v = p.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    vals.append(float(v))
        return max(vals) if vals else None

    @property
    def actual_ppi(self):
        for p in self.placements:
            v = p.get("actualPpiH")
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return None


FILES_OUTPUT_MODES = ("folder", "suffix", "in_place")


def files_output_mode(cfg):
    """Куда писать результат при входе из папки. См. files_output в config."""
    value = str(cfg.get("files_output") or "folder").strip().lower()
    return value if value in FILES_OUTPUT_MODES else "folder"


def files_output_dir(cfg, src_dir):
    """Папка для режима folder. None — режим другой."""
    if files_output_mode(cfg) != "folder":
        return None
    pattern = str(cfg.get("files_output_dir") or "{}_обработано")
    src_dir = Path(src_dir)
    name = pattern.format(src_dir.name) if "{}" in pattern else pattern
    return src_dir.parent / name


def describe_files_output(cfg, src_dir):
    mode = files_output_mode(cfg)
    if mode == "folder":
        return "separate folder %s/ (sources untouched)" % files_output_dir(cfg, src_dir).name
    if mode == "suffix":
        return "next to the source, suffix \"%s\"" % image_suffix(cfg)
    # «Без возврата» — только когда оригинал никуда не уехал. С переносом
    # в originals/ откат как раз есть, и обещать обратное значит пугать зря.
    if keep_originals_in_place(cfg):
        return "OVERWRITING the sources (no undo)"
    return ("in place of the sources (originals -> %s/)"
            % cfg.get("originals_dir_name", "originals"))


def files_as_links(cfg, paths):
    """
    Список файлов -> записи той же формы, что отдаёт collect_links.jsx.

    Так вход из папки проходит через ТОТ ЖЕ разбор, что и вход из макета:
    group_links, classify, весь цикл обработки. Писать для папки отдельный
    путь было бы ошибкой — самодельная копия пайплайна расходится с ним
    на третьей мелочи, это уже проверено.

    Геометрии у файлов нет, и это не пробел, а правда: effective PPI задаёт
    размещение в макете, которого здесь нет. Поэтому ресемплинг по нему
    и не считается — classify честно скажет, что PPI неизвестен.
    """
    out = []
    for p in paths:
        out.append({"name": Path(p).name, "status": "NORMAL",
                    "graphicClass": "Image", "filePath": str(p),
                    "placements": []})
    return out


def gather_image_files(cfg, targets):
    """Пути из аргументов -> список картинок. Папка разворачивается по маске."""
    exts = {e.lower() for e in cfg["raster_extensions"]}
    found = []
    for t in targets:
        path = Path(t)
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() in exts:
                    found.append(child.resolve())
        elif path.is_file():
            found.append(path.resolve())
    seen, uniq = set(), []
    for f in found:
        if str(f) not in seen:
            seen.add(str(f)); uniq.append(f)
    return uniq


def group_links(links, cfg):
    """Связи -> кандидаты, сгруппированные по файлу. Плюс список пропущенных."""
    groups, skipped = {}, []

    for lnk in links:
        name = lnk.get("name") or "?"
        status = lnk.get("status")
        gclass = lnk.get("graphicClass")
        fpath = lnk.get("filePath") or ""

        if status != "NORMAL" and status != "OUT_OF_DATE":
            skipped.append((name, "link status %s" % status))
            continue
        if gclass != "Image":
            skipped.append((name, "not a raster image (%s)" % gclass))
            continue
        if not fpath:
            skipped.append((name, "the link has no file path"))
            continue

        ext = Path(fpath).suffix.lower()
        if ext in [e.lower() for e in cfg["skip_extensions"]]:
            if ext in vector_extensions(cfg):
                # Растеризовать нельзя, но живой текст и плашки в них проверяются
                # отдельно — см. секцию «Векторные связи».
                skipped.append((name, "vector format %s — no raster work, "
                                      "checked for text and spot colours" % ext))
            else:
                skipped.append((name, "format %s cannot be processed" % ext))
            continue
        if ext not in [e.lower() for e in cfg["raster_extensions"]]:
            skipped.append((name, "format %s is not in the processed list" % (ext or "no extension")))
            continue
        if not Path(fpath).exists():
            skipped.append((name, "file not found on disk: %s" % fpath))
            continue

        key = str(Path(fpath).resolve())
        cand = groups.setdefault(key, Candidate(key))
        cand.placements.append(lnk)
        if fpath not in cand.link_paths:
            cand.link_paths.append(fpath)

    return list(groups.values()), skipped


def classify(cand, cfg, target_desc, doc_dir=None):
    """Заполняет reasons / warnings / needs_* / resize_factor."""
    info = cand.probe
    cs = info["cs"]
    icc = (info.get("icc_desc") or "").strip()

    # --- цвет ---
    if color_direction(cfg) == "to_rgb":
        # Обратный перевод. Целевое пространство — RGB, значит трогаем всё,
        # что им не является; RGB в чужом профиле тоже приводим к целевому.
        rgb_icc = target_rgb_icc(cfg)
        rgb_desc = icc_description(cfg, rgb_icc) if rgb_icc else ""
        if cs in ("CMYK", "GRAY"):
            cand.reasons.append(msg("to_rgb", cs=cs,
                                    icc=Path(rgb_icc).name if rgb_icc else "?"))
            cand.needs_color = True
        elif rgb_desc and icc and icc != rgb_desc:
            cand.reasons.append(msg("rgb_foreign_icc", icc=icc))
            cand.needs_color = True
        elif not info["has_icc"]:
            cand.reasons.append(msg("rgb_no_icc"))
            cand.needs_color = True
    elif cs == "CMYK":
        if not info["has_icc"]:
            cand.reasons.append(msg("cmyk_no_icc"))
            cand.warnings.append(msg("cmyk_no_icc_warn"))
            cand.needs_color = True
        elif icc != target_desc:
            cand.reasons.append(msg("cmyk_foreign_icc", icc=icc))
            cand.needs_color = True
    elif cs == "GRAY":
        if cfg.get("convert_grayscale_to_cmyk"):
            cand.reasons.append(msg("gray_to_cmyk",
                                    icc=Path(target_icc_for(cfg, cs)).name))
            cand.needs_color = True
            # Предупреждения про отсутствие встроенного ICC нет намеренно —
            # см. такой же комментарий в ветке RGB ниже.
        else:
            cand.warnings.append(msg("gray_kept"))
    else:  # RGB / Lab
        cand.reasons.append(msg("not_cmyk", cs=cs,
                                 icc=' / "%s"' % icc if icc else ""))
        cand.needs_color = True
        # Про отсутствие встроенного ICC предупреждения нет намеренно: без
        # профиля приходит подавляющее большинство файлов, и строка забивала
        # лог, ничего не сообщая. Какой профиль принят за исходный, всё равно
        # видно — build_command пишет это отдельной строкой "план".

    # --- разрешение ---
    #
    #   effective PPI          что делаем
    #   ─────────────────────  ─────────────────────────────────────────────
    #   > max (350)            уменьшить до target (300)
    #   [upscale_below, max]   не трогать — уже в норме
    #   [upscale_min, 290)     увеличить до target (300) интерполяцией
    #   < upscale_min (200)    не трогать, вынести в отчёт: нужен ИИ-апскейл
    #
    eff = cand.min_effective_ppi
    max_ppi = float(cfg["max_effective_ppi"])
    target_ppi = float(cfg["target_effective_ppi"])
    up_below = float(cfg.get("upscale_below_ppi") or 0)
    up_min = float(cfg.get("upscale_min_ppi") or 0)

    if eff is None:
        # Без вёрстки размещения нет по устройству — молчим. Предупреждать
        # стоит только если макет есть, а геометрию он не отдал: вот это
        # действительно странно.
        if not cfg.get("__on_files"):
            cand.warnings.append(msg("ppi_unknown"))
    elif eff > max_ppi:
        cand.needs_resample = True
        cand.resample_kind = "downsample"
        cand.resize_factor = target_ppi / eff
        cand.reasons.append(msg("ppi_high", ppi=eff, max=max_ppi))
    elif up_min and eff < up_min:
        # Интерполяция здесь уже не спасёт — пробуем Real-ESRGAN.
        cand.needs_ai_upscale = eff
        ok, why = ai_available(cfg)
        reject = ai_reject_reason(cand, cfg) if ok else None
        if ok and reject is None:
            cand.needs_resample = True
            cand.resample_kind = "ai_upscale"
            cand.resize_factor = target_ppi / eff
            cand.reasons.append(msg("ppi_low_ai", ppi=eff, min=up_min))
            cand.warnings.append(msg("ppi_low_ai_warn", ppi=eff, target=target_ppi))
        else:
            cand.ai_skip_reason = reject or why
            # Reason, а не только warning: иначе classify() вернёт bool(reasons)
            # == False, и файл с разрешением ниже upscale_min_ppi без всякой
            # обработки уедет в "соответствует требованиям" — ровно то, чем он
            # не является. reasons ничего не резюмирует и не пытается лечить
            # разрешение (ИИ-апскейл всё равно недоступен) — только не даёт
            # файлу молча выпасть из списка проблемных.
            cand.reasons.append(msg("ppi_low_no_ai", ppi=eff, min=up_min,
                                    why=cand.ai_skip_reason))
            cand.warnings.append(msg("ppi_low_no_ai_warn", ppi=eff, min=up_min,
                                     why=cand.ai_skip_reason))
    elif up_below and eff < up_below:
        cand.needs_resample = True
        cand.resample_kind = "upsample"
        cand.resize_factor = target_ppi / eff
        cand.reasons.append(msg("ppi_below", ppi=eff, below=up_below))
        cand.warnings.append(msg("ppi_below_warn", ppi=eff, target=target_ppi))

    if cand.needs_resample and cand.max_effective_ppi and cand.max_effective_ppi - eff > 1.0:
        cand.warnings.append(msg("ppi_mixed", min=eff, max=cand.max_effective_ppi,
                                 target=target_ppi))

    # Цепочка может не содержать этих этапов. Тогда «надо» превращается
    # в «не делаем», но ПРИЧИНА остаётся в reasons: файл не должен молча
    # выпасть в «соответствует требованиям» — он ему не соответствует,
    # просто в этом прогоне мы этим не занимаемся.
    # Выключенный этап — НЕ предупреждение: это осознанный выбор человека,
    # и цепочка целиком уже написана в шапке отчёта. Повторять её на каждой
    # картинке значит топить в шуме настоящие находки.
    if cand.needs_color and not stage_on(cfg, "convert"):
        cand.needs_color = False
    if cand.needs_resample and not stage_on(cfg, "resample"):
        cand.needs_resample = False
        cand.resample_kind = None
        cand.resize_factor = 1.0

    # --- плашечные каналы (Pantone и прочие) ---
    #
    # Плашка — повод обработать файл, даже если по цвету и разрешению он в норме:
    # в печать должен уйти CMYK. Свести её может только Photoshop, поэтому если
    # перевод выключен или невозможен, файл не отдаётся и в magick — тот просто
    # выбросил бы лишние каналы, и краска пропала бы молча.
    if cand.spot_info:
        names = ", ".join(cand.spot_info["spots"][:6])
        if want_spot_convert(cfg):
            cand.needs_spot_merge = True
            cand.reasons.append("spot channels (%s) -> CMYK" % names)
            cand.warnings.append(
                "плашки сводятся в CMYK через Photoshop (%s) — цвет плашки в CMYK "
                "передаётся приблизительно, проверьте результат" % names)
        else:
            cand.skip_reason = ("есть плашечные каналы (%s), а их перевод в CMYK "
                                "выключен — файл не обрабатывается, иначе краска "
                                "потерялась бы" % names)
            return False

    # --- предупреждения по содержимому ---
    ext = cand.path.suffix.lower()
    if ext == ".psd":
        cand.warnings.append(msg("psd_flatten"))
    clip = {p.get("clippingType") for p in cand.placements}
    risky = clip & {"PHOTOSHOP_PATH", "ALPHA_CHANNEL"}
    if risky:
        cand.warnings.append(
            "the layout uses a clipping path (%s) — check it after processing"
            % ", ".join(sorted(risky)))
    if info.get("has_alpha"):
        cand.warnings.append(
            "the file has an alpha channel — it is written as TIFF even when "
            "JPEG is set: JPEG cannot store transparency")

    ah = cand.placements[0].get("actualPpiH")
    av = cand.placements[0].get("actualPpiV")
    if isinstance(ah, (int, float)) and isinstance(av, (int, float)) and abs(ah - av) > 0.5:
        cand.warnings.append(msg("non_square_px", h=ah, v=av))

    # Файл вне папки документа мог быть подключён и к другим макетам —
    # перенос оригинала сломает связь у них.
    if doc_dir is not None and cfg.get("originals_mode") == "move":
        if not cand.path.is_relative_to(doc_dir):
            cand.warnings.append(
                "файл лежит ВНЕ папки документа (%s) — если он подключён к другим "
                "макетам, перенос оригинала сломает там связь. Запустите с "
                "--copy-originals, если это возможно" % cand.path.parent)

    return bool(cand.reasons)


# --------------------------------------------------------------------------- #
#  Проверка вылетов под обрез
# --------------------------------------------------------------------------- #

# Насколько близко к линии реза должно подходить ВИДИМОЕ СОДЕРЖИМОЕ, чтобы
# считать, что картинка задумана «в обрез».
#
# Ориентироваться на фрейм нельзя: верстальщики сплошь и рядом оставляют фреймы
# с запасом, и разворотное фото с фреймом ниже картинки давало ложное
# срабатывание («низ -20.9 мм»). Если содержимое обрывается заметно внутри
# полосы — оно в обрез и не шло, проверять там нечего.
BLEED_TOUCH_TOLERANCE_MM = 1.0

# Допуск на сравнение с нормой: без него вылет ровно в 5 мм ловится как
# недостаточный из-за погрешности вычислений с плавающей точкой.
BLEED_EPSILON_MM = 0.05

_EDGE_NAMES = {"top": "верх", "left": "лево", "bottom": "низ", "right": "право"}


def _bounds(lnk, prefix):
    """Габариты из JSON collect_links: [top, left, bottom, right] или None."""
    vals = [lnk.get(prefix + k) for k in ("Top", "Left", "Bottom", "Right")]
    if any(v is None or not isinstance(v, (int, float)) for v in vals):
        return None
    return [float(v) for v in vals]


def check_bleed(links, cfg):
    """
    Ищет размещения, у которых не хватает вылета под обрез.

    Считается по фактически видимому содержимому — пересечению картинки и её
    фрейма: даже если фрейм уходит за обрез, картинка внутри может до края не
    доставать, и на резаке вылезет белая полоса.

    Проверяются только стороны, где содержимое доходит до линии реза, — только
    там вылет и нужен. Фрейм используется лишь для диагноза «кто короче».

    Проверка геометрическая, файлы не читаются — работает и в --dry-run.
    """
    required = float(cfg.get("bleed_required_mm") or 0)
    if required <= 0:
        return []
    per_page = str(cfg.get("bleed_scope", "spread")).lower() == "page"

    issues = []
    for lnk in links:
        if lnk.get("status") in ("EMBEDDED", None):
            continue
        img = _bounds(lnk, "img")
        frame = _bounds(lnk, "frame")
        trim = _bounds(lnk, "page" if per_page else "spread")
        if img is None or frame is None or trim is None:
            continue
        if lnk.get("boundsReliable") is False:
            continue  # повёрнутый/наклонённый объект — габаритная рамка врёт

        # Видимое содержимое = пересечение картинки и фрейма.
        content = [max(frame[0], img[0]), max(frame[1], img[1]),
                   min(frame[2], img[2]), min(frame[3], img[3])]

        # Насколько объект выходит за обрез с каждой стороны (мм).
        def out(box, edge):
            if edge == "top":
                return trim[0] - box[0]
            if edge == "left":
                return trim[1] - box[1]
            if edge == "bottom":
                return box[2] - trim[2]
            return box[3] - trim[3]           # right

        bad = []
        for edge in ("top", "left", "bottom", "right"):
            content_out = out(content, edge)
            # Содержимое обрывается внутри полосы — эта сторона не в обрез.
            if content_out < -BLEED_TOUCH_TOLERANCE_MM:
                continue
            frame_out = out(frame, edge)
            if content_out < required - BLEED_EPSILON_MM:
                bad.append({
                    "edge": edge,
                    "edgeName": _EDGE_NAMES[edge],
                    "contentBleedMm": round(content_out, 2),
                    "frameBleedMm": round(frame_out, 2),
                    # Ограничитель — тот, кто короче: либо картинка не заполняет
                    # фрейм, либо сам фрейм не доходит до нужного вылета.
                    "cause": ("картинка во фрейме"
                              if content_out < frame_out - 0.01 else "фрейм"),
                })

        if bad:
            issues.append({
                "name": lnk.get("name"),
                "filePath": lnk.get("filePath"),
                "page": lnk.get("page"),
                "layer": lnk.get("layer"),
                "graphicClass": lnk.get("graphicClass"),
                "status": lnk.get("status"),
                "required": required,
                "scope": "страницы" if per_page else "разворота",
                "edges": bad,
            })
    return issues


def log_bleed_issues(issues, data, cfg):
    if not stage_on(cfg, "bleed"):
        return
    required = float(cfg.get("bleed_required_mm") or 0)
    doc_bleed = [data.get(k) for k in
                 ("bleedTopMm", "bleedLeftMm", "bleedBottomMm", "bleedRightMm")]
    have = [v for v in doc_bleed if isinstance(v, (int, float))]

    log.info("")
    if have:
        # Порядок из InDesign: Top / InsideOrLeft / Bottom / OutsideOrRight
        log.info("Document bleed settings: top %.1f / inside %.1f / bottom %.1f / outside %.1f mm",
                 *[v if isinstance(v, (int, float)) else 0 for v in doc_bleed])
        if max(have) + 0.01 < required:
            log.warning("The document bleed is below the required %.1f mm — "
                        "check Document Setup.", required)

    if not issues:
        log.info("Bleed: all placements are fine (required %.1f mm).", required)
        return

    log.warning("--- Insufficient bleed (%d placements, required %.1f mm) ---",
                len(issues), required)
    for it in issues:
        parts = ["%s %.1f мм (%s)" % (e["edgeName"], e["contentBleedMm"], e["cause"])
                 for e in it["edges"]]
        log.warning("    page %-4s %s", it.get("page") or "?", it["name"])
        log.warning("             %s", "; ".join(parts))


# --------------------------------------------------------------------------- #
#  Разбор файлов: живой текст и плашечные (spot) цвета
# --------------------------------------------------------------------------- #
#
# Только чтение — ничего не меняет, поэтому работает и в --dry-run.
# Внешних зависимостей нет, форматы разбираются вручную:
#
#   PDF и .ai (внутри PDF)  объекты и распакованные Flate-потоки:
#                           /Type/Font и /BaseFont — живой текст,
#                           /Separation и /DeviceN — плашки,
#                           /Type/Page — число страниц, /MediaBox — геометрия;
#   EPS и .ai (старый, PS)  DSC-комментарии (%%DocumentFonts,
#                           %%DocumentCustomColors, %%CMYKCustomColor)
#                           и операторы показа строк;
#   SVG                     элементы <text>/<tspan>;
#   TIFF                    теги 262 (Separated), 332/333/334 (InkSet/InkNames);
#   PSD/PSB                 заголовок (режим, число каналов) и ресурсы
#                           1006/1045 (имена доп. каналов) и 1077 (тип канала).
#
# Разбор намеренно «поверхностный»: цель — найти повод для предупреждения,
# а не построить полную модель документа. Ошибка в сторону лишнего
# предупреждения безопаснее, чем пропущенная плашка в типографии.

MAX_SCAN_BYTES = 256 * 1024 * 1024      # больше от файла не читаем
MAX_INFLATE_BYTES = 96 * 1024 * 1024    # суммарный потолок распаковки потоков PDF
MAX_STREAMS = 4000                      # сколько потоков PDF вообще смотрим

# Имена, которые в /Separation и /DeviceN означают не плашку, а процессную
# краску или служебный колорант.
PROCESS_COLORANTS = {"Cyan", "Magenta", "Yellow", "Black", "All", "None",
                     "DeviceCMYK", "DeviceRGB", "DeviceGray", "Gray",
                     "Red", "Green", "Blue"}

_RE_PDF_BASEFONT = re.compile(rb"/(?:BaseFont|FontName)\s*/([^\s/<>\[\]()]{1,200})")
_RE_PDF_FONTOBJ = re.compile(rb"/Type\s*/Font\b")
_RE_PDF_TEXTOP = re.compile(rb"(?:\)|\])\s*(?:Tj|TJ|'|\")")
_RE_PDF_PAGEOBJ = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_RE_PDF_PAGESOBJ = re.compile(rb"/Type\s*/Pages\b")
_RE_PDF_COUNT = re.compile(rb"/Count\s+(\d+)")
_RE_PDF_SEPARATION = re.compile(rb"/Separation\s*/([^\s/<>\[\]()]{1,200})")
_RE_PDF_DEVICEN = re.compile(rb"/DeviceN\s*\[([^\]]{0,2000})\]")
_RE_PDF_NAME = re.compile(rb"/([^\s/<>\[\]()]{1,200})")
_RE_PDF_MEDIABOX = re.compile(
    rb"/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)")

_RE_PS_DSC = re.compile(rb"^%%([\w+-]+):[ \t]*(.*)$")
_RE_PS_PAREN = re.compile(rb"\(((?:[^()\\]|\\.)*)\)")
_RE_SVG_TEXT = re.compile(rb"<\s*(?:text|tspan|textPath)[\s/>]", re.IGNORECASE)
_RE_SVG_FONT = re.compile(rb"font-family\s*[:=]\s*[\"']?([^\"';<]{1,120})")

# Ключи DSC, после которых перечислены шрифты. Часть из них универсальные
# (%%DocumentNeededResources: font Helvetica), поэтому значение проверяется
# на префикс "font".
_PS_FONT_KEYS = {"DocumentFonts", "DocumentNeededFonts", "DocumentSuppliedFonts",
                 "IncludeFont"}
_PS_RESOURCE_KEYS = {"DocumentNeededResources", "DocumentSuppliedResources",
                     "IncludeResource", "BeginResource"}
_PS_SPOT_KEYS = {"DocumentCustomColors", "CMYKCustomColor", "RGBCustomColor",
                 "CustomColor"}


def _read_bounded(path, limit=MAX_SCAN_BYTES):
    """Читает файл, но не больше limit байт. Возвращает (данные, обрезано?)."""
    size = Path(path).stat().st_size
    with open(path, "rb") as fh:
        data = fh.read(limit)
    return data, size > len(data)


def _pdf_name(raw):
    """Имя PDF: #XX -> символ, дальше utf-8."""
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i:i + 1] == b"#" and i + 2 < len(raw) + 1:
            try:
                out.append(int(raw[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out += raw[i:i + 1]
        i += 1
    return out.decode("utf-8", "replace")


def _font_label(raw):
    """Имя шрифта без префикса подмножества: ABCDEF+Helvetica -> Helvetica."""
    name = _pdf_name(raw)
    return re.sub(r"^[A-Z]{6}\+", "", name)


def _pdf_streams(data):
    """Распакованные Flate-потоки PDF (в т.ч. ObjStm, где прячутся шрифты)."""
    pos, used, seen = 0, 0, 0
    while seen < MAX_STREAMS and used < MAX_INFLATE_BYTES:
        i = data.find(b"stream", pos)
        if i < 0:
            break
        j = i + 6
        if data[j:j + 2] == b"\r\n":
            j += 2
        elif data[j:j + 1] in (b"\n", b"\r"):
            j += 1
        end = data.find(b"endstream", j)
        if end < 0:
            break
        pos = end + 9
        chunk = data[j:end]
        if not chunk:
            continue
        seen += 1
        try:
            # Не Flate (JPEG, шрифт, картинка) — decompressobj просто откажется.
            out = zlib.decompressobj().decompress(chunk, MAX_INFLATE_BYTES - used)
        except zlib.error:
            continue
        if not out:
            continue
        used += len(out)
        yield out


def scan_pdf(data):
    """Живой текст, плашки, число страниц и MediaBox в PDF / .ai."""
    fonts, spots, counts = set(), set(), []
    state = {"fontObjects": False, "textOps": False, "pages": 0, "mediaBox": None}

    def sweep(buf):
        for m in _RE_PDF_BASEFONT.finditer(buf):
            fonts.add(_font_label(m.group(1)))
        if not state["fontObjects"] and _RE_PDF_FONTOBJ.search(buf):
            state["fontObjects"] = True
        if not state["textOps"] and _RE_PDF_TEXTOP.search(buf):
            state["textOps"] = True
        for m in _RE_PDF_SEPARATION.finditer(buf):
            name = _pdf_name(m.group(1))
            if name not in PROCESS_COLORANTS:
                spots.add(name)
        for m in _RE_PDF_DEVICEN.finditer(buf):
            for mm in _RE_PDF_NAME.finditer(m.group(1)):
                name = _pdf_name(mm.group(1))
                if name not in PROCESS_COLORANTS:
                    spots.add(name)
        state["pages"] += len(_RE_PDF_PAGEOBJ.findall(buf))
        # Число страниц берём из /Count корневого узла /Type/Pages: счёт объектов
        # /Type/Page врёт на файлах с инкрементальными правками (объект остаётся
        # в файле дважды). Ключи в словаре идут в любом порядке, поэтому /Count
        # ищем в окне вокруг /Type/Pages.
        for m in _RE_PDF_PAGESOBJ.finditer(buf):
            window = buf[max(0, m.start() - 400):m.start() + 400]
            c = _RE_PDF_COUNT.search(window)
            if c:
                counts.append(int(c.group(1)))
        if state["mediaBox"] is None:
            m = _RE_PDF_MEDIABOX.search(buf)
            if m:
                state["mediaBox"] = [float(x) for x in m.groups()]

    sweep(data)
    for chunk in _pdf_streams(data):
        sweep(chunk)

    return {
        "kind": "pdf",
        "fonts": sorted(fonts),
        "hasText": bool(fonts) or state["fontObjects"],
        "textOps": state["textOps"],
        "spots": sorted(spots),
        "pageCount": max(counts) if counts else (state["pages"] or None),
        "mediaBox": state["mediaBox"],
    }


def scan_postscript(data):
    """Живой текст и плашки в EPS / старом .ai — по DSC-комментариям."""
    fonts, spots = set(), set()

    # DSC живут в шапке и (при "(atend)") в трейлере — смотрим оба конца.
    zones = [data[:2 * 1024 * 1024]]
    if len(data) > 4 * 1024 * 1024:
        zones.append(data[-2 * 1024 * 1024:])

    def take(key, val):
        val = val.strip()
        if not val or val.lower().startswith(b"(atend)"):
            return
        if key in _PS_FONT_KEYS:
            for name in val.split():
                fonts.add(name.decode("latin-1"))
        elif key in _PS_RESOURCE_KEYS:
            if not val.lower().startswith(b"font"):
                return
            for name in val[4:].split():
                fonts.add(name.decode("latin-1"))
        elif key in _PS_SPOT_KEYS:
            for m in _RE_PS_PAREN.finditer(val):
                name = m.group(1).decode("latin-1").strip()
                if name and name not in PROCESS_COLORANTS:
                    spots.add(name)

    for zone in zones:
        key = None
        for raw in zone.split(b"\n"):
            line = raw.strip(b"\r")
            if not line.startswith(b"%%"):
                continue
            if line.startswith(b"%%+"):
                if key:                      # продолжение предыдущего ключа
                    take(key, line[3:])
                continue
            m = _RE_PS_DSC.match(line)
            if not m:
                key = None
                continue
            key = m.group(1).decode("latin-1")
            take(key, m.group(2))

    # Признак текста — только объявленные в файле шрифты. Искать операторы
    # показа строк («(...) show») бесполезно: пролог Adobe их сам определяет
    # через них же, и любой EPS выглядел бы текстовым. Зато DSC-список шрифтов
    # PostScript-генераторы заполняют честно — на него смотрят и препресс-утилиты.
    return {
        "kind": "ps",
        "fonts": sorted(fonts),
        "hasText": bool(fonts),
        "textOps": None,
        "spots": sorted(spots),
        "pageCount": 1,
        "mediaBox": None,
    }


def scan_svg(data):
    fonts = set()
    for m in _RE_SVG_FONT.finditer(data[:4 * 1024 * 1024]):
        fonts.add(m.group(1).decode("utf-8", "replace").strip())
    has_text = bool(_RE_SVG_TEXT.search(data))
    return {"kind": "svg", "fonts": sorted(fonts), "hasText": has_text,
            "textOps": has_text, "spots": [], "pageCount": 1, "mediaBox": None}


def scan_vector_file(path):
    """
    Живой текст и плашки в векторном файле. Формат определяется по сигнатуре,
    а не по расширению: .ai бывает и PDF, и PostScript.
    """
    path = Path(path)
    try:
        data, truncated = _read_bounded(path)
    except OSError as exc:
        return {"kind": "unknown", "error": str(exc), "fonts": [], "hasText": None,
                "spots": [], "pageCount": None, "mediaBox": None, "truncated": False}

    head = data[:1024]
    if head.startswith(b"\xc5\xd0\xd3\xc6") and len(data) > 30:
        # EPS с бинарным заголовком DOS: в нём смещение и длина PS-части.
        off, length = struct.unpack("<II", data[4:12])
        if 0 < off < len(data):
            data = data[off:off + length] if length else data[off:]
            head = data[:1024]

    if head.startswith(b"%PDF"):
        res = scan_pdf(data)
    elif head.startswith(b"%!") or b"%%BoundingBox" in head:
        res = scan_postscript(data)
    elif b"<svg" in data[:4096].lower() or path.suffix.lower() == ".svg":
        res = scan_svg(data)
    elif path.suffix.lower() == ".pdf":
        res = scan_pdf(data)              # PDF без сигнатуры в начале файла
    else:
        res = {"kind": "unknown", "fonts": [], "hasText": None, "textOps": False,
               "spots": [], "pageCount": None, "mediaBox": None}

    res["truncated"] = truncated
    res["error"] = None
    return res


# --- растр: плашечные каналы ---------------------------------------------- #

_TIFF_TAGS = {262: "photometric", 277: "samples", 332: "inkset",
              333: "inknames", 334: "numberofinks", 338: "extrasamples",
              34377: "photoshop"}


def _tiff_ifd(fh, end, offset, bigtiff):
    """Читает одну IFD и возвращает {имя тега: значение}."""
    tags = {}
    fh.seek(offset)
    if bigtiff:
        count = struct.unpack(end + "Q", fh.read(8))[0]
        entry_size, count_fmt, off_fmt = 20, end + "Q", end + "Q"
    else:
        count = struct.unpack(end + "H", fh.read(2))[0]
        entry_size, count_fmt, off_fmt = 12, end + "I", end + "I"
    if count > 4096:
        return tags
    raw = fh.read(entry_size * count)

    type_size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
                 11: 4, 12: 8, 16: 8, 17: 8, 18: 8}
    for i in range(count):
        ent = raw[i * entry_size:(i + 1) * entry_size]
        if len(ent) < entry_size:
            break
        tag, typ = struct.unpack(end + "HH", ent[:4])
        if tag not in _TIFF_TAGS:
            continue
        n = struct.unpack(count_fmt, ent[4:4 + (8 if bigtiff else 4)])[0]
        payload = ent[4 + (8 if bigtiff else 4):]
        nbytes = n * type_size.get(typ, 1)
        if nbytes > len(payload):
            off = struct.unpack(off_fmt, payload[:8 if bigtiff else 4])[0]
            if off <= 0 or nbytes > 1 << 20:
                continue
            keep = fh.tell()
            fh.seek(off)
            value = fh.read(nbytes)
            fh.seek(keep)
        else:
            value = payload[:nbytes]

        name = _TIFF_TAGS[tag]
        if typ == 2:                       # ASCII
            tags[name] = value
        elif typ == 3:                     # SHORT
            tags[name] = list(struct.unpack(end + "H" * n, value[:2 * n]))
        elif typ == 4:                     # LONG
            tags[name] = list(struct.unpack(end + "I" * n, value[:4 * n]))
        else:
            tags[name] = value
    return tags


def scan_tiff_spots(path):
    """Плашечные краски в TIFF: Separated + больше четырёх красок."""
    with open(path, "rb") as fh:
        head = fh.read(16)
        if head[:2] == b"II":
            end = "<"
        elif head[:2] == b"MM":
            end = ">"
        else:
            return None
        magic = struct.unpack(end + "H", head[2:4])[0]
        if magic == 42:
            bigtiff = False
            offset = struct.unpack(end + "I", head[4:8])[0]
        elif magic == 43:
            bigtiff = True
            offset = struct.unpack(end + "Q", head[8:16])[0]
        else:
            return None
        tags = _tiff_ifd(fh, end, offset, bigtiff)

    photometric = (tags.get("photometric") or [None])[0]
    samples = (tags.get("samples") or [None])[0]
    inkset = (tags.get("inkset") or [1])[0]
    inks = (tags.get("numberofinks") or [None])[0]
    names = []
    raw_names = tags.get("inknames")
    if isinstance(raw_names, (bytes, bytearray)):
        names = [n.decode("latin-1").strip() for n in raw_names.split(b"\x00") if n.strip()]

    if photometric != 5:                   # не Separated — плашек быть не может
        return None

    if inkset == 2:
        # InkSet = 2: набор красок вообще не CMYK — считаем плашками всё.
        return {"spots": names or ["non-standard ink set"],
                "channels": samples, "inkSet": inkset,
                "numberOfInks": inks or samples, "mode": "Separated"}

    # Каналы сверх четырёх — это либо доп. краска, либо альфа. Что именно,
    # сказано в ExtraSamples: 0 = «не определено» (у Photoshop так помечена
    # плашка), 1 и 2 — ассоциированная и неассоциированная альфа.
    extras = tags.get("extrasamples") or []
    if not isinstance(extras, list):
        extras = []

    # Photoshop кладёт в TIFF свой блок ресурсов (тег 34377) — тот же, что
    # в PSD. Имён красок в InkNames он при этом не пишет, зато в ресурсах
    # есть и название плашки, и признак «плашка / альфа».
    ps_names, ps_flags = [], None
    ps_blob = tags.get("photoshop")
    if isinstance(ps_blob, (bytes, bytearray)):
        try:
            ps_res = _psd_resources(bytes(ps_blob))
            ps_names = _psd_alpha_names(ps_res)
            ps_flags = _psd_spot_flags(ps_res)
        except Exception:
            ps_names, ps_flags = [], None

    spots = []
    for i in range(max(0, (samples or 4) - 4)):
        if (extras[i] if i < len(extras) else 0) != 0:
            continue                       # это альфа, а не краска
        if ps_flags is not None and i < len(ps_flags) and not ps_flags[i]:
            continue                       # Photoshop говорит, что это альфа
        if len(names) > 4 + i:
            spots.append(names[4 + i])
        elif i < len(ps_names):
            spots.append(ps_names[i])
        else:
            spots.append("без имени #%d" % (i + 1))
    if not spots:
        return None
    return {"spots": spots, "channels": samples, "inkSet": inkset,
            "numberOfInks": inks or samples, "mode": "Separated"}


_PSD_MODE_NAMES = {0: "Bitmap", 1: "Grayscale", 2: "Indexed", 3: "RGB", 4: "CMYK",
                   7: "Multichannel", 8: "Duotone", 9: "Lab"}
_PSD_MODE_CHANNELS = {0: 1, 1: 1, 2: 1, 3: 3, 4: 4, 7: 0, 8: 1, 9: 3}


def _psd_resources(data):
    """Ресурсы 8BIM: {id: данные}."""
    out, pos = {}, 0
    while pos + 12 <= len(data):
        if data[pos:pos + 4] != b"8BIM":
            break
        rid = struct.unpack(">H", data[pos + 4:pos + 6])[0]
        pos += 6
        nlen = data[pos]
        pos += 1 + nlen
        if (nlen + 1) % 2:
            pos += 1                        # имя дополняется до чётной длины
        if pos + 4 > len(data):
            break
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        out[rid] = data[pos:pos + size]
        pos += size + (size % 2)
    return out


def _psd_alpha_names(res):
    names = []
    raw = res.get(1045)                     # Unicode Alpha Names
    if raw:
        pos = 0
        while pos + 4 <= len(raw):
            n = struct.unpack(">I", raw[pos:pos + 4])[0]
            pos += 4
            if n <= 0 or pos + n * 2 > len(raw):
                break
            names.append(raw[pos:pos + n * 2].decode("utf-16-be", "replace").rstrip("\x00"))
            pos += n * 2
    if names:
        return names
    raw = res.get(1006)                     # Alpha Channel Names (Pascal-строки)
    if raw:
        pos = 0
        while pos < len(raw):
            n = raw[pos]
            pos += 1
            names.append(raw[pos:pos + n].decode("latin-1", "replace"))
            pos += n
    return names


def _psd_spot_flags(res):
    """
    DisplayInfo (ресурс 1077): по записи на каждый дополнительный канал —
    2 байта цветовое пространство, 8 байт цвет, 2 байта непрозрачность,
    1 байт режим: 0 — альфа, 1 — инвертированная альфа, 2 — плашка.
    """
    raw = res.get(1077)
    if not raw or len(raw) < 4:
        return None
    version = struct.unpack(">I", raw[:4])[0]
    if version != 1:
        return None
    flags, pos = [], 4
    while pos + 13 <= len(raw):
        mode = raw[pos + 12]
        flags.append(mode == 2)
        pos += 13
    return flags


def scan_psd_spots(path):
    """Плашечные каналы в PSD/PSB: доп. каналы с типом spot, Duotone, Multichannel."""
    with open(path, "rb") as fh:
        head = fh.read(26)
        if head[:4] != b"8BPS":
            return None
        channels, height, width, depth, mode = struct.unpack(">HIIHH", head[12:26])
        cm_len = struct.unpack(">I", fh.read(4))[0]
        fh.seek(cm_len, 1)
        res_len = struct.unpack(">I", fh.read(4))[0]
        res_raw = fh.read(min(res_len, 32 * 1024 * 1024))

    res = _psd_resources(res_raw)
    names = _psd_alpha_names(res)
    flags = _psd_spot_flags(res)
    base = _PSD_MODE_CHANNELS.get(mode, 0)
    extra = max(0, channels - base)

    spots = []
    if mode == 8:                            # Duotone — краски заданы в файле
        spots = names or ["Duotone"]
    elif mode == 7:                          # Multichannel — все каналы плашечные
        spots = names or ["канал %d" % (i + 1) for i in range(channels)]
    elif extra:
        for i in range(extra):
            is_spot = flags[i] if flags and i < len(flags) else None
            if is_spot is False:
                continue                     # обычная альфа
            nm = names[i] if i < len(names) else "доп. канал %d" % (i + 1)
            if is_spot is None and not _looks_like_spot(nm):
                continue                     # тип канала неизвестен, имя не похоже
            spots.append(nm)

    if not spots:
        return None
    return {"spots": spots, "channels": channels, "extraChannels": extra,
            "mode": _PSD_MODE_NAMES.get(mode, str(mode))}


def _looks_like_spot(name):
    low = (name or "").lower()
    return any(k in low for k in ("pantone", "spot", "плашк", "hks", "toyo",
                                 "varnish", "лак", "silver", "gold", "foil"))


def scan_raster_spots(path):
    """Плашечные каналы растрового файла или None. Ошибки чтения не бросает."""
    ext = Path(path).suffix.lower()
    try:
        if ext in (".tif", ".tiff"):
            return scan_tiff_spots(path)
        if ext in (".psd", ".psb"):
            return scan_psd_spots(path)
    except Exception as exc:
        log.debug("Could not check spot colours in %s: %s", path, exc)
    return None


# --------------------------------------------------------------------------- #
#  Мост в Illustrator и Photoshop
# --------------------------------------------------------------------------- #
#
# InDesign вызывается через `do script ... language javascript`, а у Illustrator
# и Photoshop команда своя — `do javascript`. Параметры так же передаются
# файлом (см. комментарий у JSX_PARAMS_PATH), у каждого приложения свой.

AI_PARAMS_PATH = JSX_PARAMS_PATH.with_name("ai_params.json")
PS_PARAMS_PATH = JSX_PARAMS_PATH.with_name("ps_params.json")


_APP_DIRS = ["/Applications", str(Path.home() / "Applications")]


def app_path(app_id):
    """
    Путь к приложению по bundle id или None, если оно не установлено.

    LaunchServices об этом не спрашиваем: `path to application id` в AppleScript
    умеет ЗАПУСТИТЬ приложение и подвесить вызов, а здесь нужна просто проверка
    «стоит или нет». Поэтому читаем Info.plist в /Applications напрямую.
    """
    want = str(app_id).lower()
    for root in _APP_DIRS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            bundles = sorted(base.glob("*.app")) + sorted(base.glob("*/*.app"))
        except OSError:
            continue
        for app in bundles:
            try:
                with open(app / "Contents" / "Info.plist", "rb") as fh:
                    bid = plistlib.load(fh).get("CFBundleIdentifier", "")
            except Exception:
                continue
            if str(bid).lower() == want:
                return str(app)
    return None


def run_adobe_jsx(cfg, app_id, jsx_path, params, params_path, out_path, purpose):
    """Гоняет .jsx в Illustrator/Photoshop и возвращает разобранный JSON-отчёт."""
    jsx_path = Path(jsx_path).resolve()
    if not jsx_path.exists():
        raise FileNotFoundError("Script not found: %s" % jsx_path)
    if Path(out_path).exists():
        Path(out_path).unlink()

    params = dict(params)
    params["outPath"] = str(out_path)
    nonce = write_jsx_params(params, params_path)

    # Форма именно `do javascript file "<posix-путь>"`. Всё остальное Illustrator
    # и Photoshop понимают по-разному: `POSIX file` внутри tell разбирает само
    # приложение, и Photoshop падает с -1728, а alias — с общей ошибкой 8800.
    script = (
        'with timeout of {timeout} seconds\n'
        '  tell application id {appid}\n'
        '    activate\n'
        '    do javascript file {jsx}\n'
        '  end tell\n'
        'end timeout\n'
    ).format(
        timeout=int(cfg.get("applescript_timeout_sec", 3600)),
        appid=_as_quote(app_id),
        jsx=_as_quote(str(jsx_path)),
    )
    log.debug("AppleScript (%s):\n%s", purpose, script)
    proc = subprocess.run(["osascript", "-"], input=script,
                          capture_output=True, text=True)
    if proc.returncode != 0 and not Path(out_path).exists():
        raise RuntimeError("%s did not run %s.\nosascript stderr: %s"
                           % (app_id, jsx_path.name, proc.stderr.strip()))
    data = _read_jsx_json(out_path)
    _check_nonce(data, nonce, purpose)
    return data


# --------------------------------------------------------------------------- #
#  Векторные связи: живой текст и плашки
# --------------------------------------------------------------------------- #

DEFAULT_VECTOR_EXTENSIONS = [".ai", ".eps", ".pdf", ".svg"]

# Что Illustrator открывает и сохраняет обратно в том же формате.
OUTLINE_FORMATS = (".ai", ".eps", ".pdf")


def vector_extensions(cfg):
    return [e.lower() for e in (cfg.get("vector_extensions") or DEFAULT_VECTOR_EXTENSIONS)]


def text_opts(cfg):
    return cfg.get("text_in_links") or {}


def spot_opts(cfg):
    return cfg.get("spot_colors") or {}


# У каждой из двух проверок две ступени:
#   enabled = false             — не проверять вовсе, как будто функции нет;
#   convert_* = false           — проверять и предупреждать, но не править.
# Поэтому «проверяем» и «переводим» — разные вопросы, и спрашивать их надо
# порознь: иначе выключенный перевод заодно прятал бы и предупреждения.

def check_text(cfg):
    return bool(text_opts(cfg).get("enabled", True)) and stage_on(cfg, "text")


def check_spots(cfg):
    return bool(spot_opts(cfg).get("enabled", True)) and stage_on(cfg, "spots")


def want_outlines(cfg):
    return bool(check_text(cfg) and text_opts(cfg).get("convert_to_outlines", True))


def want_spot_convert(cfg):
    return bool(check_spots(cfg) and spot_opts(cfg).get("convert_to_cmyk", True))


class VectorCandidate(object):
    """Векторная связь (.ai / .eps / .pdf / .svg) и всё, что про неё известно."""

    def __init__(self, path):
        self.path = Path(path)
        self.link_paths = []
        self.placements = []
        self.scan = {}
        self.actions = []        # "outlines" и/или "spots"
        self.blocked = None      # почему автоматом не переводим
        self.out_path = None
        self.result = None
        self.notes = []          # что сообщил Illustrator

    @property
    def has_text(self):
        return bool(self.scan.get("hasText"))

    @property
    def spots(self):
        return self.scan.get("spots") or []

    @property
    def fonts(self):
        return self.scan.get("fonts") or []

    @property
    def pages(self):
        return sorted({str(p.get("page") or "?") for p in self.placements})

    def summary(self):
        parts = []
        if self.has_text:
            parts.append("text" + (": %s" % ", ".join(self.fonts[:4]) if self.fonts else ""))
        if self.spots:
            parts.append("spots: %s" % ", ".join(self.spots[:6]))
        return "; ".join(parts) or "clean"


def group_vector_links(links, cfg):
    """Векторные связи -> кандидаты, сгруппированные по файлу на диске."""
    exts = vector_extensions(cfg)
    groups = {}
    for lnk in links:
        if lnk.get("status") not in ("NORMAL", "OUT_OF_DATE"):
            continue
        fpath = lnk.get("filePath") or ""
        if not fpath or Path(fpath).suffix.lower() not in exts:
            continue
        if not Path(fpath).exists():
            continue
        key = str(Path(fpath).resolve())
        cand = groups.setdefault(key, VectorCandidate(key))
        cand.placements.append(lnk)
        if fpath not in cand.link_paths:
            cand.link_paths.append(fpath)
    return sorted(groups.values(), key=lambda c: c.path.name.lower())


def plan_vector(cand, cfg):
    """Что делать с файлом в боевом прогоне: кривые, плашки или ничего."""
    need_text = cand.has_text and want_outlines(cfg)
    need_spots = bool(cand.spots) and want_spot_convert(cfg)
    if not (need_text or need_spots):
        return

    ext = cand.path.suffix.lower()
    if ext not in OUTLINE_FORMATS:
        cand.blocked = ("формат %s Illustrator не открывает/не сохраняет обратно — "
                        "переведите вручную" % ext)
        return
    if cand.scan.get("kind") == "unknown":
        cand.blocked = "не удалось разобрать формат файла — переведите вручную"
        return

    if ext == ".pdf":
        # Illustrator открывает из PDF ровно одну страницу, поэтому многостраничные
        # и «вставлена страница N» не трогаем — иначе в макет уедет не та страница.
        pages = cand.scan.get("pageCount")
        if pages and pages > 1:
            cand.blocked = ("многостраничный PDF (%d стр.) — Illustrator откроет "
                            "только одну страницу; переведите вручную" % pages)
            return
        placed = sorted({p.get("pdfPageNumber") for p in cand.placements
                         if isinstance(p.get("pdfPageNumber"), (int, float))
                         and int(p.get("pdfPageNumber")) != 1})
        if placed:
            cand.blocked = ("в макет вставлена страница %s этого PDF — автоперевод "
                            "только для одностраничных; переведите вручную"
                            % ", ".join(str(int(p)) for p in placed))
            return

    if need_text:
        cand.actions.append("outlines")
    if need_spots:
        cand.actions.append("spots")


def vector_out_path(cand, cfg):
    """Имя нового файла: рядом с оригиналом, с пометкой о том, что сделано."""
    suffix = ""
    if "outlines" in cand.actions:
        suffix += text_opts(cfg).get("outlined_suffix") or "_outlined"
    if "spots" in cand.actions:
        suffix += spot_opts(cfg).get("spot_suffix") or "_cmyk"
    stem = cand.path.stem
    if suffix and stem.endswith(suffix):
        suffix = ""                       # уже помечен — не плодим _outlined_outlined
    return unique_path(cand.path.with_name(stem + suffix + cand.path.suffix))


def log_vector_findings(cands, cfg, dry_run):
    """Секция лога про живой текст и плашки в векторных связях."""
    log.info("")
    if not (check_text(cfg) or check_spots(cfg)):
        log.info("Vector links were not checked: both checks are disabled "
                 "in config.json (text_in_links.enabled, spot_colors.enabled).")
        return
    if not cands:
        log.info("No vector links (%s) in the document.",
                 ", ".join(vector_extensions(cfg)))
        return
    what = " и ".join([w for w, on in (("живой текст", check_text(cfg)),
                                       ("spots", check_spots(cfg))) if on])

    flagged = [c for c in cands if c.has_text or c.spots]
    log.info("--- Vector links: %d files, checked for %s, with findings: %d ---",
             len(cands), what, len(flagged))
    for c in cands:
        if not (c.has_text or c.spots):
            log.info("    OK  %s — checked for %s, nothing found",
                     c.path.name, what)
            continue
        log.warning("    !!  %s (page %s)", c.path.name, ", ".join(c.pages) or "?")
        if c.has_text:
            log.warning("        live text%s",
                        ": " + ", ".join(c.fonts) if c.fonts else
                        " (шрифты не перечислены в файле)")
        if c.spots:
            log.warning("        spot colours: %s", ", ".join(c.spots))
        if c.scan.get("truncated"):
            log.warning("        file over %d MB — only the beginning was checked",
                        MAX_SCAN_BYTES // (1024 * 1024))
        if c.blocked:
            log.warning("        MANUAL WORK : %s", c.blocked)
        elif c.actions:
            what = []
            if "outlines" in c.actions:
                what.append("text -> outlines")
            if "spots" in c.actions:
                what.append("spots -> CMYK")
            log.warning("        %s: %s", "plan" if dry_run else "doing", ", ".join(what))
        else:
            log.warning("        auto-convert disabled in settings — warning only")


def run_outline_stage(cfg, cands, doc_dir, originals_dir, workdir, mapping, target_desc):
    """
    Боевой прогон: Illustrator переводит текст в кривые и/или плашки в CMYK,
    оригинал уезжает в originals, связь переподключается на новый файл.
    """
    jobs = [c for c in cands if c.actions]
    if not jobs:
        return None

    app_id = text_opts(cfg).get("illustrator_app_id") or "com.adobe.illustrator"
    if app_path(app_id) is None:
        for c in jobs:
            c.blocked = ("Illustrator не найден (%s) — переведите вручную" % app_id)
            c.actions = []
        log.error("")
        log.error("Illustrator not found (%s): %d vector files left unchanged.",
                  app_id, len(jobs))
        return None

    log.info("")
    log.info("--- Illustrator: outline text and spots to CMYK (%d files) ---", len(jobs))
    log.info("WARNING: Illustrator converts spots using its own working CMYK.")
    log.info("Check Illustrator > Settings > Color Settings: the working CMYK must be")
    log.info('"%s" (%s).', target_desc, cfg["target_icc"])

    payload = []
    for c in jobs:
        c.out_path = vector_out_path(c, cfg)
        box = c.scan.get("mediaBox")
        job = {
            "src": str(c.path),
            "dst": str(c.out_path),
            "format": c.path.suffix.lower().lstrip("."),
            "outlineText": "outlines" in c.actions,
            "convertSpots": "spots" in c.actions,
        }
        # Illustrator открывает PDF по одной из его рамок (Media/Crop/Trim/...).
        # Если он возьмёт не ту, монтажная область не совпадёт с MediaBox —
        # тогда файл лучше не трогать, чем сдвинуть картинку в макете.
        if job["format"] == "pdf" and box:
            job["expectedWidthPt"] = round(abs(box[2] - box[0]), 2)
            job["expectedHeightPt"] = round(abs(box[3] - box[1]), 2)
        payload.append(job)

    try:
        report = run_adobe_jsx(cfg, app_id, JSX_DIR / "outline_text.jsx",
                               {"jobs": payload}, AI_PARAMS_PATH,
                               Path(workdir) / "outline_report.json",
                               "перевод текста в кривые")
    except Exception as exc:
        log.error("Illustrator failed: %s", exc)
        for c in jobs:
            c.result = {"status": "error", "error": str(exc)}
        return None

    by_src = {}
    for r in report.get("results", []):
        by_src[str(r.get("src"))] = r

    for c in jobs:
        r = by_src.get(str(c.path))
        log.info("")
        log.info("    %s", c.path.name)
        if r is None:
            c.result = {"status": "error", "error": "Illustrator не вернул результат"}
            log.error("        ERROR: Illustrator returned no result for this file")
            continue

        for w in (r.get("warnings") or "").split("|"):
            if w.strip():
                c.notes.append(w.strip())
                log.warning("        ! %s", w.strip())

        if not r.get("ok"):
            c.result = {"status": "error", "error": r.get("error") or "не выполнено"}
            c.blocked = r.get("error") or "Illustrator не смог обработать файл"
            log.error("        ERROR: %s — file left unchanged", c.result["error"])
            continue

        out = Path(r.get("dst") or c.out_path)
        if not out.exists():
            c.result = {"status": "error", "error": "Illustrator не создал %s" % out}
            log.error("        ERROR: %s", c.result["error"])
            continue

        if r.get("outlined"):
            log.info("        text outlined, objects: %s", r["outlined"])
        if r.get("spotsConverted"):
            log.info("        spots to CMYK: %s", r["spotsConverted"])
        if r.get("remainingText"):
            log.warning("        ! text objects left: %s — check manually",
                        r["remainingText"])

        dest = None
        if not keep_originals_in_place(cfg):
            dest = originals_target(c, doc_dir, originals_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if cfg["originals_mode"] == "move":
                shutil.move(str(c.path), str(dest))
            else:
                shutil.copy2(str(c.path), str(dest))

        c.out_path = out
        c.result = {
            "status": "ok",
            "out": str(out),
            "original_moved_to": str(dest) if dest else None,
            "originalKeptInPlace": dest is None,
            "outlined": r.get("outlined"),
            "spotsConverted": r.get("spotsConverted"),
            "documentColorSpace": r.get("documentColorSpace"),
            "warnings": c.notes,
        }
        if dest is None:
            log.info("        original    : kept in place (%s)", c.path.name)
        else:
            log.info("        original    : %s -> %s",
                     "moved" if cfg["originals_mode"] == "move" else "copied",
                     dest.relative_to(doc_dir) if dest.is_relative_to(doc_dir) else dest)
        log.info("        new file    : %s", out.name)
        for raw in c.link_paths:
            mapping.append({"oldPath": raw, "newPath": str(out)})

    return report


# --------------------------------------------------------------------------- #
#  Плашечные каналы в растре: Photoshop сводит их в CMYK
# --------------------------------------------------------------------------- #

# Форматы, у которых вообще бывают плашечные каналы.
SPOT_RASTER_FORMATS = (".tif", ".tiff", ".psd", ".psb")

_PS_INTENTS = {"perceptual": "PERCEPTUAL", "relative": "RELATIVECOLORIMETRIC",
               "saturation": "SATURATION", "absolute": "ABSOLUTECOLORIMETRIC"}

COLOR_ENGINES = ("magick", "photoshop")

# Общий выключатель: чем обрабатывать растр целиком.
#
#   photoshop — вариант 1: цветоделение, памятные цвета и общий тон делает Adobe.
#   native    — вариант 2: ImageMagick (цвет и кривые), Apple Vision (маски)
#               и Core Image (общий тон). Adobe не нужен вовсе.
#
# Этапы читают своё значение, и слово auto у этапа значит «как в engine».
# Своё значение этап сохраняет всегда: общий выключатель его не перебивает —
# иначе нельзя было бы, скажем, делить цвет в Photoshop, а маски брать у Vision.
PIPELINE_ENGINES = ("photoshop", "native")


# --------------------------------------------------------------------------- #
#  Этапы прогона: «кубики», из которых собирается цепочка
# --------------------------------------------------------------------------- #
#
# Состав цепочки настраивается, ПОРЯДОК — нет. Порядок в этом кортеже и есть
# порядок прогона, и каждая связь в нём оплачена пойманным багом, а не
# соображением о стройности:
#
#   * обрезка идёт первой — всё дальнейшее не должно работать по пикселям,
#     которых в макете не видно (на номере это минус 18% пикселей в среднем);
#   * цветоделение только ПОСЛЕ ИИ-апскейла: Real-ESRGAN не понимает ICC
#     и работает по RGB;
#   * тон и память только ДО цветоделения — правка кладётся в исходном
#     пространстве, вторая конверсия теряет данные;
#   * ресемплинг, цветоделение, шарпинг и запись склеены в ОДНУ команду
#     magick намеренно: каждый лишний проход заново квантует, а финальное
#     ужатие вдобавок усредняет шум, поднятый правкой.
#
# Поэтому цепочка собирается ВЫБОРОМ, а не перестановкой: сочетание, которое
# нарушило бы что-то из перечисленного, скрипт отвергает с объяснением.
#
# Поля: имя, область ("document" — на весь макет, "image" — на каждый файл),
# от чего зависит, что делает.
PIPELINE_STAGES = (
    ("pasteboard",      "document", (),          "delete images left on the pasteboard"),
    ("links",           "document", (),          "read links and geometry from InDesign"),
    ("bleed",          "document", ("links",),  "check the bleed past the trim"),
    ("text",           "document", ("links",),  "outline live text in vector links"),
    ("spots",          "document", ("links",),  "convert spot colours to process"),
    ("crop",         "image",    ("links",),  "crop to the frame, scale to 100%"),
    ("upscale",         "image",    (),          "AI upscale for small images"),
    ("lift",            "image",    (),          "lift underexposed frames: exposure, shadows, saturation"),
    ("tone",             "image",    (),          "overall tone: white point and exposure"),
    ("memory",          "image",    (),          "memory colours: skin, sky, greenery"),
    ("master",          "image",    (),          "master for the tablet edition"),
    ("convert",    "image",    (),          "convert colour using the target profile"),
    ("resample",      "image",    (),          "bring resolution to the target"),
    ("sharpen",         "image",    (),          "compensating sharpening"),
    ("relink", "document", ("links",),  "relink and save the document"),
)

STAGE_ORDER = tuple(name for name, _s, _d, _t in PIPELINE_STAGES)

# Прежние русские имена. Этапы и цепочки переименованы в английские, но
# конфиги и записанные команды с русскими именами продолжают работать:
# молча ломать чужой рабочий файл ради красоты идентификаторов нельзя.
STAGE_ALIASES = {
    "связи": "links", "вылеты": "bleed", "текст": "text", "плашки": "spots",
    "обрезка": "crop", "апскейл": "upscale", "тон": "tone", "память": "memory",
    "мастер": "master", "цветоделение": "convert", "ресемплинг": "resample",
    "шарпинг": "sharpen", "переподключение": "relink",
}
PIPELINE_ALIASES = {"вёрстка": "layout", "верстка": "layout",
                    "цвет": "color", "геометрия": "geometry"}


def stage_id(name):
    """Имя этапа -> канонический идентификатор."""
    name = str(name or "").strip()
    return STAGE_ALIASES.get(name, name)


def pipeline_id(name):
    """Имя цепочки -> каноническое."""
    name = str(name or "").strip()
    return PIPELINE_ALIASES.get(name, name)
STAGE_INFO = {name: (scope, deps, title)
              for name, scope, deps, title in PIPELINE_STAGES}


def pipeline_presets(cfg):
    """Именованные цепочки из config. Пустой словарь — значит только «вёрстка»."""
    got = cfg.get("pipelines")
    return got if isinstance(got, dict) else {}


def resolve_pipeline(cfg, name=None, stages=None, has_document=True):
    """
    Состав цепочки: имя пресета или явный список -> кортеж этапов В ПОРЯДКЕ.

    Проверяет то, что проверить можно заранее: неизвестные имена, этап
    без того, от чего он зависит, и этапы, которым нужен макет, когда
    макета нет. Всё это дешевле поймать здесь, чем на середине прогона.
    """
    presets = pipeline_presets(cfg)
    if stages:
        want = [stage_id(x) for x in stages if str(x).strip()]
        source = "stage list"
    else:
        name = pipeline_id(name or cfg.get("pipeline") or "layout")
        if name not in presets:
            raise RuntimeError(
                "Unknown chain \"%s\". Available: %s"
                % (name, ", ".join(sorted(presets)) or "none (no pipelines section "
                   "in config.json)"))
        want = [stage_id(x) for x in (presets[name] or [])]
        source = "chain \"%s\"" % name

    unknown = [x for x in want if x not in STAGE_INFO]
    if unknown:
        raise RuntimeError("%s: unknown stages %s. Known: %s"
                           % (source, ", ".join(unknown), ", ".join(STAGE_ORDER)))
    chosen = set(want)
    for stage in want:
        missing = [d for d in STAGE_INFO[stage][1] if d not in chosen]
        if missing:
            raise RuntimeError(
                "%s: stage \"%s\" is impossible without \"%s\" — %s"
                % (source, stage, ", ".join(missing),
                   "frame geometry comes only from the layout"
                   if "links" in missing else "nothing to work with"))
    if not has_document:
        need_doc = [x for x in want if STAGE_INFO[x][0] == "document"]
        if need_doc:
            raise RuntimeError(
                "%s: stages %s work only with an InDesign layout, but the input is "
                "files. Remove them from the chain or give an .indd."
                % (source, ", ".join(need_doc)))
    elif "links" not in chosen:
        raise RuntimeError(
            "%s: the input is an InDesign layout, but the chain has no \"links\" "
            "stage — there is nowhere to get the image list from." % source)
    # Связь, которую не видно из списка зависимостей, потому что она зависит
    # от НАСТРОЙКИ. При обрезке с нормировкой масштаба плотность файла
    # считается так, чтобы картинка встала во фрейм РОВНО 100% (см. расчёт
    # new_density). Поставить этот масштаб может только переподключение;
    # без него InDesign оставит прежний масштаб, и картинка окажется в макете
    # не того физического размера. Молча пересчитать плотность по-другому
    # было бы хуже: человек просил 100%, а получил бы тихо не то.
    if "crop" in chosen and "relink" not in chosen \
            and want_scale_100(cfg):
        raise RuntimeError(
            "%s: \"crop\" with scale normalisation requires \"relink\". The file "
            "density is computed for a placement at exactly 100%%, and only "
            "InDesign can set that scale. Either add \"relink\" to the chain "
            "or switch off crop_to_frame.scale_to_100." % source)
    if not chosen:
        raise RuntimeError("%s: the chain is empty, nothing to do." % source)
    return tuple(x for x in STAGE_ORDER if x in chosen)


def stage_on(cfg, name):
    """
    Включён ли этап в цепочке этого прогона.

    Цепочка кладётся в cfg под ключом «__stages» один раз, при разборе
    аргументов. Если её там нет — значит зовут не из прогона (панель
    настроек, проверка конфига), и врать «выключено» нельзя: считаем,
    что включено всё.
    """
    stages = cfg.get("__stages")
    return True if stages is None else (name in stages)


def describe_pipeline(stages):
    """Строка для шапки лога: что включено, а что пропущено."""
    off = [x for x in STAGE_ORDER if x not in stages]
    line = " -> ".join(stages)
    if off:
        line += "   (skipped: %s)" % ", ".join(off)
    return line


def pipeline_engine(cfg):
    value = str(cfg.get("engine") or "photoshop").strip().lower()
    return value if value in PIPELINE_ENGINES else "photoshop"


def _stage_engine(cfg, value, by_pipeline, allowed, fallback):
    """Значение этапа с поддержкой auto. by_pipeline: {engine: значение этапа}."""
    value = str(value or "auto").strip().lower()
    if value == "auto":
        value = by_pipeline[pipeline_engine(cfg)]
    return value if value in allowed else fallback


TONE_ENGINE_TITLES = {"own": "own arithmetic (Photoshop)",
                      "photoshop": "Photoshop auto levels",
                      "coreimage": "Apple Core Image"}


def describe_engine(cfg):
    """Строка «чем что делается» для шапки лога и отчёта."""
    whole = ("option 1 — everything on Adobe" if pipeline_engine(cfg) == "photoshop"
             else "option 2 — without Adobe")
    parts = ["colour: %s" % ("Photoshop" if color_engine(cfg) == "photoshop"
                           else "magick"),
             "memory: %s" % ("Photoshop" if memory_engine(cfg) == "photoshop"
                             else "Vision + magick"),
             "tone: %s" % TONE_ENGINE_TITLES.get(tone_engine(cfg), tone_engine(cfg))]
    return "%s (%s)" % (whole, ", ".join(parts))


def color_engine(cfg):
    """Кто делит цвет: "magick" или "photoshop"."""
    return _stage_engine(cfg, cfg.get("color_engine"),
                         {"photoshop": "photoshop", "native": "magick"},
                         COLOR_ENGINES, "magick")


def photoshop_app_id(cfg):
    # Ключ внутри spot_colors появился раньше общего и мог быть переопределён
    # руками — уважаем его, иначе включение плашек молча уехало бы на другой
    # экземпляр Photoshop, чем цветоделение.
    return (spot_opts(cfg).get("photoshop_app_id")
            or cfg.get("photoshop_app_id")
            or "com.adobe.Photoshop")


# Строки, по которым узнаётся исчерпанный рабочий диск Photoshop. Ошибка
# приходит как «General Photoshop error», и отличить её от прочих можно только
# по тексту.
SCRATCH_FULL_MARKS = ("scratch disk", "рабочие диски", "scratch disks")


def is_scratch_full(exc):
    text = str(exc).lower()
    return any(m in text for m in SCRATCH_FULL_MARKS)


def restart_photoshop(cfg, why=""):
    """
    Перезапустить Photoshop, чтобы он отдал рабочий диск.

    Зачем это вообще. Scratch-файл живёт СТОЛЬКО ЖЕ, СКОЛЬКО СЕССИЯ: Photoshop
    отдаёт его только при выходе. Пайплайн держит одну сессию на весь прогон,
    и на номере из сотни картинок она съедает десятки гигабайт. На машине
    с 15 ГБ свободного места прогон умирал на 39-й картинке, а дальше сыпался
    подряд — приложение из этого состояния само не выходит.

    Выход просим вежливо, а если не отвечает за отведённое время — снимаем:
    к этому моменту Photoshop уже висит на модальном окне «рабочие диски
    полны», и вежливые способы до него не доходят. Своих документов у него
    в этот момент нет — JSX закрывает их сам, в том числе при сбое.

    Следующий вызов `do javascript` поднимет Photoshop заново: AppleScript
    запускает приложение сам.
    """
    if why:
        log.warning("        ! restarting Photoshop: %s", why)
    app_id = photoshop_app_id(cfg)
    subprocess.run(["osascript", "-e",
                    'tell application id "%s" to quit saving no' % app_id],
                   capture_output=True, text=True, timeout=60)
    for _ in range(20):
        alive = subprocess.run(["pgrep", "-f", "Adobe Photoshop"],
                               capture_output=True, text=True)
        if not (alive.stdout or "").strip():
            log.info("        Photoshop   : closed, scratch disk freed")
            return True
        time.sleep(1)
    subprocess.run(["pkill", "-9", "-f", "Adobe Photoshop"], capture_output=True)
    time.sleep(3)
    log.warning("        ! Photoshop did not quit — killed")
    return True


def ps_intent(cfg):
    return _PS_INTENTS.get(str(cfg.get("rendering_intent", "")).lower(),
                           "RELATIVECOLORIMETRIC")


# Какие расширения Photoshop считает своими для каждого формата.
_PS_EXTS_BY_FORMAT = {
    "TIFF": (".tif", ".tiff"),
    "PNG": (".png",),
    "JPEG": (".jpg", ".jpeg", ".jpe"),
    "PSD": (".psd",),
    "BMP": (".bmp",),
    "WEBP": (".webp",),
    "GIF": (".gif",),
}


def photoshop_ready_source(cfg, source, workdir, tag):
    """
    Приводит имя файла в соответствие с его содержимым.

    Photoshop выбирает разборщик по РАСШИРЕНИЮ. Файл, названный .png, а внутри
    лежащий TIFF-ом, он открыть отказывается: «Cannot open the file because the
    open options are incorrect». ImageMagick разбирает по сигнатуре и подмены
    не замечает — поэтому на magick такие файлы проходили молча, а на Photoshop
    падали. В вёрстке они встречаются (пойман view from the back.png).

    Возвращает (путь, пояснение). Копия делается побайтово, без перекодирования:
    содержимое и так корректно, врёт только имя.
    """
    source = Path(source)
    proc = subprocess.run(
        [cfg["magick"], "identify", "-ping", "-format", "%m", "%s[0]" % source],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return source, None
    real = (proc.stdout or "").strip().upper()
    exts = _PS_EXTS_BY_FORMAT.get(real)
    if not exts or source.suffix.lower() in exts:
        return source, None
    fixed = Path(workdir) / ("psin_%s%s" % (tag, exts[0]))
    shutil.copy2(str(source), str(fixed))
    return fixed, ('the file is named "%s" but holds %s — a copy %s was made for Photoshop'
                   % (source.name, real, fixed.name))


def run_photoshop_color(cfg, cand, source, src_icc, workdir, tag):
    """
    Делит цвет одного файла в Photoshop и возвращает путь к временному TIFF.

    Вызывается по файлу за раз, а не пакетом, потому что цветоделение обязано
    идти ПОСЛЕ ИИ-апскейла: модель не понимает ICC и работает по RGB, а апскейл
    живёт внутри основного цикла. Пакетный вызов до цикла ломал бы такие файлы.
    Один вызов — один заход `do javascript`; Photoshop при этом не закрывается,
    так что платим только за круг AppleScript.

    Возвращает Path. Ошибку поднимает исключением — решает вызывающий.
    """
    dst = Path(workdir) / ("color_%s.tif" % tag)
    dst_icc = target_icc_for(cfg, cand.probe["cs"])
    target_desc = icc_description(cfg, dst_icc)
    if not target_desc:
        raise RuntimeError('could not read the profile name from "%s"' % dst_icc)

    # Photoshop адресует профили названием, а не путём, — поэтому и исходный
    # переводим в название. Пустая строка означает «в файле есть свой ICC,
    # назначать ничего не надо».
    src_desc = ""
    if src_icc:
        src_desc = icc_description(cfg, src_icc)
        if not src_desc:
            raise RuntimeError('could not read the profile name from "%s"' % src_icc)

    source, renamed = photoshop_ready_source(cfg, source, workdir, tag)
    if renamed:
        cand.warnings.append(renamed)
        log.warning("        ! %s", renamed)

    job = {
        "src": str(source),
        "dst": str(dst),
        "profile": target_desc,
        "srcProfile": src_desc,
        "intent": ps_intent(cfg),
        "blackPointCompensation": bool(cfg.get("black_point_compensation")),
    }
    report = run_adobe_jsx(cfg, photoshop_app_id(cfg), JSX_DIR / "convert_cmyk.jsx",
                           {"jobs": [job]}, PS_PARAMS_PATH,
                           Path(workdir) / ("color_report_%s.json" % tag),
                           "цветоделение в Photoshop")

    results = report.get("results") or []
    r = results[0] if results else None
    if r is None or not r.get("ok"):
        raise RuntimeError((r or {}).get("error") or "Photoshop returned no result")
    if not dst.exists():
        raise RuntimeError("Photoshop did not write the file %s" % dst.name)

    for w in (r.get("warnings") or "").split("|"):
        if w.strip():
            cand.warnings.append(w.strip())
            log.warning("        ! %s", w.strip())
    return dst


def run_spot_merge_stage(cfg, cands, workdir, target_desc):
    """
    Сводит плашечные каналы в CMYK через Photoshop и кладёт результат во
    временный TIFF — дальше он идёт в magick вместо оригинала.

    Почему Photoshop, а не ImageMagick: свести плашку правильно — значит
    подмешать её краску в CMYK с её же кривой и наложением. ImageMagick
    лишние каналы просто выбросит, то есть краска пропадёт молча.
    """
    if not cands:
        return None
    # Безадобный вариант сюда дойти может, а сделать ничего не может: замены
    # этой операции в командной строке нет. Молча отдать файл magick нельзя —
    # он выбросит лишние каналы, и плашка исчезнет с оттиска.
    if pipeline_engine(cfg) == "native":
        for c in cands:
            c.skip_reason = ("есть плашечные каналы, а выбран вариант без Adobe — "
                             "сведите плашки вручную или переключите engine "
                             "на photoshop")
            c.result = {"status": "error", "error": c.skip_reason}
        log.error("")
        log.error("Without Adobe: %d files with spot channels were not processed "
                  "(only Photoshop can merge them).", len(cands))
        return None
    app_id = photoshop_app_id(cfg)
    if app_path(app_id) is None:
        for c in cands:
            c.skip_reason = ("есть плашечные каналы, а Photoshop не найден (%s) — "
                             "сведите плашки вручную" % app_id)
            c.result = {"status": "error", "error": c.skip_reason}
        log.error("")
        log.error("Photoshop not found (%s): %d files with spots not processed.",
                  app_id, len(cands))
        return None

    intent = ps_intent(cfg)
    jobs = []
    for n, c in enumerate(cands, 1):
        c.spot_source = Path(workdir) / ("spot_%03d.tif" % n)
        jobs.append({
            "src": str(c.path),
            "dst": str(c.spot_source),
            "profile": target_desc,
            "intent": intent,
            "blackPointCompensation": bool(cfg.get("black_point_compensation")),
        })

    log.info("")
    log.info("--- Photoshop: spot channels -> CMYK (%d files) ---", len(jobs))
    log.info('Target profile: "%s" (%s)', target_desc, cfg["target_icc"])

    try:
        report = run_adobe_jsx(cfg, app_id, JSX_DIR / "merge_spots.jsx",
                               {"jobs": jobs}, PS_PARAMS_PATH,
                               Path(workdir) / "spot_report.json",
                               "сведение плашечных каналов")
    except Exception as exc:
        log.error("Photoshop failed: %s", exc)
        for c in cands:
            c.spot_source = None
            c.skip_reason = "Photoshop failed: %s" % exc
            c.result = {"status": "error", "error": c.skip_reason}
        return None

    by_src = {str(r.get("src")): r for r in report.get("results", [])}
    for c in cands:
        r = by_src.get(str(c.path))
        if r is None or not r.get("ok") or not Path(c.spot_source or "").exists():
            why = (r or {}).get("error") or "Photoshop returned no result"
            c.spot_source = None
            c.skip_reason = ("не удалось свести плашечные каналы (%s) — файл оставлен "
                             "как был, сведите плашки вручную" % why)
            c.result = {"status": "error", "error": c.skip_reason}
            log.error("    !!  %s — %s", c.path.name, why)
            continue
        c.spot_merged = [s for s in (r.get("merged") or "").split("|") if s]
        # Файл уже в целевом профиле — второй раз цвет не делим.
        c.needs_color = False
        log.info("    OK  %s — channels merged: %s%s", c.path.name,
                 len(c.spot_merged) or r.get("mergedCount") or "?",
                 " (%s)" % ", ".join(c.spot_merged) if c.spot_merged else "")
        if r.get("warnings"):
            for w in r["warnings"].split("|"):
                if w.strip():
                    c.warnings.append(w.strip())
                    log.warning("        ! %s", w.strip())
    return report


# --------------------------------------------------------------------------- #
#  Мастер: общая заготовка для печати и планшета
# --------------------------------------------------------------------------- #
#
# Из одного номера собирают две версии: печатную (CMYK, 300 ppi) и планшетную
# (RGB, 200 ppi, отдельный проект планшетной версии). Дорогие и «умные» операции
# у них общие — обрезка по фрейму, ИИ-апскейл, правка памятных цветов, — а
# различаются только хвосты: разрешение, пространство и шарпинг.
#
# Делать общую часть дважды нельзя не только из-за времени: цветокоррекция,
# посчитанная второй раз, даст другой результат, и номер разъедется между
# печатью и планшетом. Поэтому prepress.py откладывает общую заготовку —
# МАСТЕР — рядом с документом, а планшетный инструмент берёт её готовой.
#
# Мастер — это файл ровно в том виде, в каком он входит в цветоделение:
# обрезанный, при необходимости прошедший ИИ-апскейл, с поправленными
# памятными цветами, ресемплённый до целевого разрешения печати (300),
# в ИСХОДНОМ цветовом пространстве и БЕЗ шарпинга. Шарпинг общим быть не может:
# он делается последним, по финальному разрешению, и для экрана он другой.
#
# Контракт между инструментами — папка и манифест, а не общий код: у проектов
# он намеренно раздельный (см. шапку README обоих). Манифест говорит, из
# какого исходника сделан мастер (путь, размер, время правки — по ним видно,
# не устарел ли он), что с ним успели сделать и, если файл обрезан, как его
# ставить в макет: масштаб и отступы от угла фрейма.

MASTER_SCHEMA = 1


def master_cfg(cfg):
    return cfg.get("master") or {}


def check_master(cfg):
    """Откладываем ли мастер для планшетной версии."""
    return bool(master_cfg(cfg).get("enabled")) and stage_on(cfg, "master")


def master_dir(cfg, doc_dir):
    return Path(doc_dir) / (master_cfg(cfg).get("dir_name") or "master")


def master_target(cfg, cand, mdir, used):
    """
    Имя файла мастера.

    Прошлый прогон того же макета ПЕРЕЗАПИСЫВАЕТСЯ: мастер описывает связь,
    а не момент времени, и плодить `имя_2.tif`, `имя_3.tif` от прогона
    к прогону значило бы засорять папку и оставлять в ней осиротевшие файлы,
    на которые манифест уже не ссылается. Номер добавляется только внутри
    ОДНОГО прогона — когда в вёрстке лежат одноимённые файлы из разных папок.
    """
    base = cand.path.stem
    dst = mdir / (base + ".tif")
    n = 2
    while str(dst) in used:
        dst = mdir / ("%s_%d.tif" % (base, n))
        n += 1
    used.add(str(dst))
    return dst


def write_master(cfg, cand, source, doc_dir, steps, used):
    """
    Кладёт заготовку в master/ и возвращает запись для манифеста.

    source — файл в том виде, в каком он идёт в цветоделение. Если это сам
    оригинал (ничего с ним не делали), мастер не нужен вовсе: планшетный
    инструмент возьмёт оригинал, как берёт и сейчас.
    """
    source = Path(source)
    if source == cand.path:
        return None
    mdir = master_dir(cfg, doc_dir)
    mdir.mkdir(parents=True, exist_ok=True)
    dst = master_target(cfg, cand, mdir, used)

    if source.suffix.lower() in (".tif", ".tiff"):
        shutil.copy2(str(source), str(dst))
    else:
        # Мастер всегда TIFF: он промежуточный, и терять на нём качество
        # второй раз незачем.
        proc = subprocess.run(
            [cfg["magick"], "%s[0]" % str(source), "-compress",
             cfg["tiff_compression"], str(dst)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "").strip() or "code %d" % proc.returncode)

    cand.master_path = str(dst)
    info = probe_image(cfg, dst)
    try:
        st = cand.path.stat()
        src_size, src_mtime = st.st_size, int(st.st_mtime)
    except OSError:
        src_size, src_mtime = None, None

    item = {
        "source": str(cand.path),
        "sourceSize": src_size,
        "sourceMtime": src_mtime,
        "master": dst.name,
        "pixels": [info["width"], info["height"]],
        "colorspace": info["cs"],
        "iccDescription": info.get("icc_desc"),
        "ppi": round(info["ppi_x"], 4),
        "steps": steps,
        "sharpened": False,
        "pages": sorted({str(p.get("page") or "?") for p in cand.placements}),
    }
    if cand.crop:
        item["crop"] = {
            "box": list(cand.crop["box"]),
            "widthPt": round(cand.crop["widthPt"], 4),
            "padLeftPt": round(cand.crop["padLeftPt"], 4),
            "padTopPt": round(cand.crop["padTopPt"], 4),
            "cropped": bool(cand.crop["cropped"]),
            "scaleTo100": want_scale_100(cfg),
        }
    return item


def write_master_manifest(cfg, doc_dir, doc_real, items):
    """
    Манифест мастеров: по нему планшетный инструмент понимает, что можно взять
    готовым, а что устарело. Свежесть проверяется по размеру и времени правки
    исходника: пересчитывать хеш тридцатимегабайтных кадров ради этого дорого,
    а перезаписанный файл меняет и то, и другое.
    """
    mdir = master_dir(cfg, doc_dir)
    if not items:
        return None
    mdir.mkdir(parents=True, exist_ok=True)
    path = mdir / (master_cfg(cfg).get("manifest_name") or "master.json")
    payload = {
        "schema": MASTER_SCHEMA,
        "createdAt": _dt.datetime.now().isoformat(timespec="seconds"),
        "document": str(doc_real),
        "targetEffectivePpi": cfg.get("target_effective_ppi"),
        "sharpened": False,
        "note": ("Заготовки для второй версии номера: обрезка, ИИ-апскейл и "
                 "памятные цвета уже сделаны, шарпинга нет. Цветовое "
                 "пространство исходное."),
        "items": items,
    }
    # Прогон по другому документу той же папки не должен затирать чужие записи:
    # сливаем по исходнику, свежая запись побеждает.
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
            keep = {str(i.get("source")): i for i in (old.get("items") or [])}
            for i in items:
                keep[str(i.get("source"))] = i
            payload["items"] = list(keep.values())
        except (OSError, ValueError):
            pass
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    return path


# --------------------------------------------------------------------------- #
#  Обрезка по фрейму и масштаб ровно 100%
# --------------------------------------------------------------------------- #
#
# В макет обычно кладут кадр целиком, а видно из него — то, что попало во фрейм.
# Всё остальное едет в типографию впустую: место на диске, время обработки,
# вес PDF. Обрезка оставляет видимую часть плюс запас (по умолчанию 10 мм
# с каждой стороны, где он есть) — запас нужен, чтобы картинку во фрейме можно
# было потом подвинуть.
#
# Заодно решается вторая задача: плотность нового файла считается так, чтобы
# в макете он встал РОВНО 100%. Масштаб перестаёт быть «15.34%», и любой,
# кто откроет вёрстку, видит честные цифры, а не пересчитанные.
#
# Геометрию для этого даёт collect_links.jsx в системе координат ФРЕЙМА
# (innerImg*/innerFrame*): поворот фрейма обрезке не мешает — мешает только
# поворот compute it itself картинки относительно фрейма, и его проверяем отдельно.

MM_IN_PT = 72.0 / 25.4


def crop_cfg(cfg):
    return cfg.get("crop_to_frame") or {}


def check_crop(cfg):
    """Обрезаем ли по фрейму вообще."""
    return bool(crop_cfg(cfg).get("enabled")) and stage_on(cfg, "crop")


def want_scale_100(cfg):
    """Приводим ли масштаб размещения к 100%."""
    return bool(crop_cfg(cfg).get("scale_to_100", True))


def _inner_box(p, prefix):
    """[left, top, right, bottom] в координатах фрейма или None."""
    vals = [p.get(prefix + k) for k in ("Left", "Top", "Right", "Bottom")]
    if any(v is None or not isinstance(v, (int, float)) for v in vals):
        return None
    return [float(v) for v in vals]


def crop_blocker(cand, cfg):
    """
    Почему этот файл нельзя обрезать и нормировать масштаб. None — можно.

    Каждая причина — не осторожность впрок, а случай, в котором пересчёт был бы
    неверным.
    """
    if len(cand.placements) != 1:
        return ("placed in the layout %d times — crop and 100%% scale would fit "
                "only one of them" % len(cand.placements))
    p = cand.placements[0]
    if _inner_box(p, "innerImg") is None or _inner_box(p, "innerFrame") is None:
        return "InDesign did not report geometry in frame coordinates"
    # Повёрнутый ФРЕЙМ обрезке не мешает: поворот общий у фрейма и картинки
    # и в разнице координат сокращается. Мешает поворот картинки ОТНОСИТЕЛЬНО
    # фрейма — тогда прямоугольник обрезки в пикселях не совпадает с видимой
    # областью.
    #
    # Имена свойств у InDesign инвертированы против интуиции, проверено опытом:
    # у картинки внутри фрейма rotationAngle — СУММАРНЫЙ угол вместе с фреймом,
    # а absoluteRotationAngle — поворот ОТНОСИТЕЛЬНО фрейма. Фрейм 22° +
    # картинка ещё 15° дают rotationAngle 37 и absoluteRotationAngle 15.
    rel_rot = float(p.get("absRotation") or 0)
    rel_shear = float(p.get("absShear") or 0)
    if abs(rel_rot) > 0.001 or abs(rel_shear) > 0.001:
        return "the image is rotated relative to the frame (%.3f°)" % rel_rot
    sh, sv = p.get("scaleH"), p.get("scaleV")
    if not isinstance(sh, (int, float)) or not isinstance(sv, (int, float)):
        return "placement scale unknown"
    if abs(float(sh) - float(sv)) > 0.01:
        return ("the scale differs by axis (%.2f%% x %.2f%%) — one file density "
                "cannot express that" % (sh, sv))
    if not (p.get("effectivePpiH") or 0) > 0:
        return "placement effective PPI unknown"
    return None


def plan_crop(cand, cfg):
    """
    Считает прямоугольник обрезки и положение картинки во фрейме после неё.

    Возвращает словарь или None. Ключи:
      box        — (x, y, w, h) в пикселях ИСХОДНОГО файла;
      widthPt    — ширина обрезанного куска в пунктах (физический размер
                   в макете, он же задаёт плотность нового файла);
      padLeftPt / padTopPt — где окажется левый верхний угол картинки
                   относительно левого верхнего угла фрейма; по ним relink.jsx
                   вернёт картинку на место после переподключения;
      saving     — доля отрезанных пикселей;
      cropped    — обрезали ли на самом деле (False — только масштаб 100%).

    Даже когда резать нечего, план возвращается: он нужен ради масштаба 100%,
    а обрезка в этом случае просто равна всей картинке.
    """
    blocker = crop_blocker(cand, cfg)
    if blocker is not None:
        cand.crop_note = blocker
        return None

    p = cand.placements[0]
    img = _inner_box(p, "innerImg")
    frm = _inner_box(p, "innerFrame")
    eff = float(p["effectivePpiH"])
    px_per_pt = eff / 72.0
    W = int(cand.probe["width"])
    H = int(cand.probe["height"])

    margin = float(crop_cfg(cfg).get("margin_mm", 10)) * MM_IN_PT
    if not check_crop(cfg):
        margin = None      # обрезка выключена — берём картинку целиком

    if margin is None:
        left, top, right, bottom = img
    else:
        left = max(img[0], frm[0] - margin)
        top = max(img[1], frm[1] - margin)
        right = min(img[2], frm[2] + margin)
        bottom = min(img[3], frm[3] + margin)
    if right - left <= 0 or bottom - top <= 0:
        cand.crop_note = "the visible part of the image is empty"
        return None

    x0 = max(0, min(W - 1, int(round((left - img[0]) * px_per_pt))))
    y0 = max(0, min(H - 1, int(round((top - img[1]) * px_per_pt))))
    x1 = max(x0 + 1, min(W, int(round((right - img[0]) * px_per_pt))))
    y1 = max(y0 + 1, min(H, int(round((bottom - img[1]) * px_per_pt))))
    w, h = x1 - x0, y1 - y0

    saving = 1.0 - (float(w) * h) / (float(W) * H)
    min_saving = float(crop_cfg(cfg).get("min_saving_percent", 3)) / 100.0
    cropped = check_crop(cfg) and saving >= min_saving
    if not cropped:
        # Резать не стоит — но масштаб 100% всё равно приводим, а для этого
        # нужен план по целой картинке.
        x0 = y0 = 0
        w, h = W, H
        saving = 0.0

    # Обратно в пункты — уже по фактическим целым пикселям, а не по идеалу.
    width_pt = w / px_per_pt
    pad_left = (img[0] + x0 / px_per_pt) - frm[0]
    pad_top = (img[1] + y0 / px_per_pt) - frm[1]
    return {"box": (x0, y0, w, h), "widthPt": width_pt,
            "padLeftPt": pad_left, "padTopPt": pad_top,
            "saving": saving, "cropped": cropped}


def crop_args(cand):
    """Ключи magick для обрезки. +repage обязателен: без него смещение
    остаётся в метаданных, и следующая операция считает координаты от него."""
    x, y, w, h = cand.crop["box"]
    return ["-crop", "%dx%d+%d+%d" % (w, h, x, y), "+repage"]


# --------------------------------------------------------------------------- #
#  Памятные цвета: кожа, небо, зелень
# --------------------------------------------------------------------------- #
#
# Памятный цвет — тот, у которого зритель помнит, каким он должен быть, и врёт
# не прибор, а память: кожа, небо, трава. Ошибку в них видно без оригинала
# рядом, поэтому у препресса на них есть числа, а не «на глаз».
#
# Числа печатные, в процентах краски, и проверяются они ПОСЛЕ перевода в
# target_icc — но правка кладётся в ИСХОДНОЕ пространство файла, до
# цветоделения. Иначе картинка прошла бы через конверсию дважды: один раз
# ради замера, второй — ради результата.
#
# Область находит Photoshop: небо — Select Sky, людей — Select People, кожу —
# цветовой диапазон «Тона кожи», пересечённый с людьми, зелень — цветовой
# диапазон по Lab. Ограничитель по людям здесь не перестраховка: без него
# «кожей» уверенно становятся деревянная стена и бежевый фасад (проверено на
# тестовой вёрстке — фасад отеля дал 13.9% «кожи» в кадре без единого человека).
#
# Правила и пороги живут в config.json, арифметика — в color_correct.jsx:
# подбирать их приходится по оттискам, и лазить за ними в .jsx неудобно.

MEMORY_COLOR_ORDER = ("skin", "sky", "grass")
MEMORY_COLOR_TITLES = {"skin": "skin", "sky": "sky", "grass": "greenery",
                       "tone": "общий тон"}


def tone_spec(cfg):
    """
    Раздел global_tone -> задание для color_correct.jsx.

    Общий тон — то, что ретушёр делает тремя пипетками: чёрная, серая и белая
    точки, плюс экспозиция. Снимается ПЕРВЫМ, до памятных цветов: пока на кадре
    общий увод, правила по коже и небу борются с ним, а не с местным цветом.
    """
    t = (cfg.get("global_tone") or {})
    expo = t.get("exposure") or {}
    return {
        "enabled": bool(t.get("enabled", True)) and stage_on(cfg, "tone"),
        "apply": bool(t.get("apply", True)),
        # В задание уходит РАЗРЕШЁННОЕ имя движка, а не "auto": JSX слова auto
        # не знает и молча откатывался на свою арифметику — то есть вариант
        # «всё на Adobe» на деле правил тон не Adobe. Ловится только чтением
        # лога: сбоя нет, просто работает не тот движок.
        "engine": tone_engine(cfg),
        "adobeModes": list(t.get("adobe_modes") or ["autoBlackWhite", "autoNeutrals"]),
        "highlightTarget": float(t.get("highlight_target", 248)),
        "shadowTarget": float(t.get("shadow_target", 10)),
        "highlightPercentile": float(t.get("highlight_percentile", 0.5)) / 100.0,
        "shadowPercentile": float(t.get("shadow_percentile", 0.5)) / 100.0,
        "strength": float(t.get("strength", 1.0)),
        "colorStrength": float(t.get("color_strength", 0.6)),
        "colorMaxShift": float(t.get("color_max_shift", 12)),
        "highlightCeiling": float(t.get("highlight_ceiling", 252)),
        "maxShift": float(t.get("max_shift", 18)),
        "shadowMaxShift": float(t.get("shadow_max_shift", 8)),
        "minMove": float(t.get("min_move", 3)),
        "grayPoint": bool(t.get("gray_point", True)),
        "neutralBox": list(t.get("neutral_box") or [45, 97, 15]),
        "neutralFuzziness": int(t.get("neutral_fuzziness", 25)),
        "neutralMinArea": float(t.get("neutral_min_area_percent", 1.5)),
        "neutralHighlightPercentile": float(t.get("neutral_highlight_percentile", 95)) / 100.0,
        "darkBox": list(t.get("dark_box") or [4, 34, 12]),
        "darkMinArea": float(t.get("dark_min_area_percent", 0.5)),
        "exposure": {
            "enabled": bool(expo.get("enabled", True)),
            "skinMin": float(expo.get("skin_min", 110)),
            "maxLift": float(expo.get("max_lift", 15)),
            "litPercentile": float(expo.get("lit_percentile", 75)) / 100.0,
            "holdPercentile": float(expo.get("hold_percentile", 90)) / 100.0,
        },
    }


def memory_cfg(cfg):
    return cfg.get("memory_colors") or {}


def check_memory_colors(cfg):
    """Ищем ли памятные цвета вообще."""
    return bool(memory_cfg(cfg).get("enabled")) and stage_on(cfg, "memory")


def want_color_correct(cfg):
    """Правим ли найденное, или только пишем числа в отчёт."""
    return check_memory_colors(cfg) and bool(memory_cfg(cfg).get("apply", True))


def _rng(section, key, default):
    """Пара [min, max] из конфига; None на месте границы значит «не ограничено»."""
    value = section.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        value = default
    return [None if v is None else float(v) for v in value]


def _rule(ch, kind, ref, bounds, label, aim=False):
    """
    Одно правило для движка правки.

    `aim` переводит правило из ОГРАНИЧИТЕЛЯ в ПРИЦЕЛ: вместо «сработать, если
    вышли за границу, и дотянуть до неё» — «всегда вести к середине диапазона».
    Разница не косметическая. На реальном номере (64 замера кожи) ограничитель
    сдвинул медиану Y минус M с 2.8 до 6.0 и оставил 78% лиц вне нормы 8–15,
    которую сам же проверяет: тянуть до ближней границы, да ещё и на strength
    от пути, — это (8 − 2.8) x 0.7 = 3.6, то есть почти ничего. Прицел на
    середину даёт (11.5 − 2.8) x strength и попадает внутрь нормы.

    Ограничителем правило остаётся там, где разброс законный: у травы в тени
    своя доля пурпурной, у неба на закате своя, и ровнять их под одно число
    значит убивать замысел кадра. У кожи законного разброса нет — есть число,
    в которое её ставят руками.
    """
    r = {"ch": ch, "kind": kind, "label": label}
    if ref:
        r["ref"] = ref
    lo, hi = bounds
    if lo is not None:
        r["min"] = lo
    if hi is not None:
        r["max"] = hi
    # Прицел имеет смысл только когда известны обе границы: середины
    # у полуоткрытого диапазона нет.
    if aim and lo is not None and hi is not None:
        r["aim"] = (lo + hi) / 2.0
    return r


def memory_color_specs(cfg):
    """
    Разделы config.json -> задания для color_correct.jsx.

    «Ворота» (gates) только отсеивают область целиком: детектор ошибается, и
    ошибается предсказуемо — Select Sky отдаёт белый потолок в интерьере и
    туманное море, цветовой диапазон зелени — сукно бильярдного стола. Ворота
    смотрят на печатные числа области и молча отбрасывают такие находки.
    Правила (rules) уже правят: тянут канал до ближайшей нарушенной границы.
    """
    m = memory_cfg(cfg)
    specs = []

    skin = m.get("skin") or {}
    skin_aim = bool(skin.get("aim", True))
    if skin.get("enabled", True):
        specs.append({
            "name": "skin",
            "detector": "skin",
            "minArea": float(skin.get("min_area_percent", 0.5)),
            "fuzziness": int(skin.get("fuzziness", 40)),
            "detectFaces": bool(skin.get("detect_faces", True)),
            "peopleTags": list(skin.get("people_tags") or []),
            "perPerson": bool(skin.get("per_person", True)),
            "gates": [
                _rule("M", "abs", None, _rng(skin, "m_range", [10, 80]),
                      "пурпурная в области"),
            ],
            "rules": [
                _rule("C", "ratio", "M", _rng(skin, "c_of_m", [0.15, 0.45]),
                      "C to M (skin greyness)", aim=skin_aim),
                _rule("Y", "diff", "M", _rng(skin, "y_minus_m", [0, 15]),
                      "Y minus M (tan)", aim=skin_aim),
            ],
        })

    sky = m.get("sky") or {}
    if sky.get("enabled", True):
        specs.append({
            "name": "sky",
            "detector": "sky",
            "minArea": float(sky.get("min_area_percent", 3.0)),
            "gates": [
                _rule("C", "abs", None, _rng(sky, "c_range", [12, 100]),
                      "голубая в области"),
                _rule("C", "diff", "M", [float(sky.get("c_minus_m_min", 8)), None],
                      "C минус M (синева)"),
                _rule("Y", "ratio", "C", [None, float(sky.get("y_of_c_gate_max", 0.45))],
                      "Y to C (yellow in sky)"),
            ],
            "rules": [
                _rule("M", "ratio", "C", _rng(sky, "m_of_c", [0.25, 0.55]),
                      "M to C (sky purpleness)"),
                _rule("Y", "abs", None, [None, float(sky.get("y_max", 10))],
                      "Y in sky (dirt)"),
            ],
        })

    grass = m.get("grass") or {}
    if grass.get("enabled", True):
        specs.append({
            "name": "grass",
            "detector": "lab",
            "minArea": float(grass.get("min_area_percent", 3.0)),
            "fuzziness": int(grass.get("fuzziness", 45)),
            "labMin": list(grass.get("lab_min") or [20, -45, 5]),
            "labMax": list(grass.get("lab_max") or [80, -8, 60]),
            "gates": [
                _rule("C", "abs", None, _rng(grass, "c_range", [10, 100]),
                      "голубая в области"),
                _rule("Y", "diff", "M", [float(grass.get("y_minus_m_min", 15)), None],
                      "Y minus M (is it green)"),
            ],
            "rules": [
                _rule("Y", "ratio", "C", _rng(grass, "y_of_c", [1.2, 2.5]),
                      "Y к C (жёлтая в зелени)"),
                _rule("M", "ratio", "C", _rng(grass, "m_of_c", [0, 0.55]),
                      "M to C (dirt in greenery)"),
            ],
        })
    return specs


def describe_memory_colors(cfg):
    """Строка «что ищем и что делаем» для шапки лога и отчёта."""
    if not check_memory_colors(cfg):
        return "check disabled (memory_colors.enabled = false)"
    names = ", ".join(MEMORY_COLOR_TITLES[s["name"]] for s in memory_color_specs(cfg))
    if not names:
        return "all colours switched off individually"
    what = ("measure and correct to norm" if want_color_correct(cfg)
            else "measure and report only (apply = false)")
    return "%s — %s, at most %g%% ink per run" % (
        names, what, float(memory_cfg(cfg).get("max_shift_percent", 12)))


# Помощник на Swift собирается setup.sh и живёт рядом с Real-ESRGAN.
# Путь тот же, что у остальных помощников (см. TOOLS_DIR в native_color):
# приложение подставляет свой бандл одной переменной окружения.
CORE_IMAGE_TOOL = Path(os.environ.get("PREPRESS_TOOLS_DIR")
                       or (Path.home() / "Library/Application Support"
                           "/prepress_automation/tools")) / "vtone"


def tone_engine(cfg):
    """
    Чем править общий тон: own | photoshop | coreimage.

    Читает конфиг НАПРЯМУЮ, а не через tone_spec: tone_spec кладёт в задание
    уже разрешённое значение, и обращение к нему отсюда закольцевало бы вызов.
    """
    return _stage_engine(cfg, (cfg.get("global_tone") or {}).get("engine"),
                         {"photoshop": "photoshop", "native": "coreimage"},
                         ("own", "photoshop", "coreimage"), "own")


def memory_engine(cfg):
    """Чем искать области памятных цветов: photoshop | native."""
    return _stage_engine(cfg, memory_cfg(cfg).get("engine"),
                         {"photoshop": "photoshop", "native": "native"},
                         ("photoshop", "native"), "photoshop")


def run_core_image_tone(cfg, source, workdir, tag):
    """
    Авто-тон средствами Core Image. Возвращает (путь, описание) или (None, причина).

    Идёт ОТДЕЛЬНЫМ процессом, до Photoshop: это единственный движок, которому
    Adobe не нужен вовсе. Внутри — то, что Apple зовёт auto enhance: баланс
    по лицам, вибрация, тон-кривая, подтяжка светов и теней. Чисел наружу
    он не отдаёт, поэтому «до» и «после» замеряем сами.
    """
    if not CORE_IMAGE_TOOL.exists():
        return None, ("the Core Image helper is not built (%s) — run ./setup.sh"
                      % CORE_IMAGE_TOOL.name)
    dst = Path(workdir) / ("citone_%s.tif" % tag)
    proc = subprocess.run([str(CORE_IMAGE_TOOL), str(source), str(dst)],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        return None, ((proc.stdout or proc.stderr or "").strip()[:160]
                      or "code %d" % proc.returncode)
    return dst, (proc.stdout or "").strip()


def limit_foreign_tone(cfg, source, toned, workdir, tag):
    """
    Ограничить чужой авто-тон: светлоту взять целиком, цвет — долей.

    Зачем. Авто-алгоритмы правят светлоту и цвет заодно, и цветовая часть у них
    самая рискованная: Core Image балансирует белую точку по лицам
    (`CIFaceBalance`) и уводит В ТЁПЛОЕ ВЕСЬ кадр. Замер на реальном номере:
    140/139/101 -> 151/137/93, то есть синей на 8 меньше, красной на 11 больше
    по всему полю. Лицам это на пользу, а трава на том же кадре желтеет — и
    именно это было первой претензией к прогону.

    Разложение делается в Lab: L — светлота, a и b — цвет. Светлота берётся
    у движка целиком (там контраст и проработка теней, ради которых его
    и зовут), цвет подмешивается на `color_strength`. При color_strength = 0
    остаётся ровно тональная работа и ни грамма увода.

    Тот же раздел `global_tone` и тот же ключ, что у своего движка: у человека
    должна быть ОДНА ручка на «насколько трогать цвет», а не по одной на движок.
    """
    t = cfg.get("global_tone") or {}
    light = max(0.0, min(1.0, float(t.get("strength", 1.0))))
    color = max(0.0, min(1.0, float(t.get("color_strength", 0.6))))
    if light >= 1.0 and color >= 1.0:
        return toned, ""            # ограничивать нечего
    work = Path(workdir)
    lab_src = work / ("tlab_o_%s.miff" % tag)
    lab_new = work / ("tlab_t_%s.miff" % tag)
    mixed = work / ("tmix_%s.miff" % tag)
    dst = work / ("tlimit_%s.tif" % tag)
    mg = cfg["magick"]
    # 16 бит намеренно: Lab -> RGB и обратно в восьми битах даёт полосы
    # на плавном небе.
    subprocess.run([mg, "%s[0]" % str(source), "-colorspace", "Lab", "-depth", "16",
                    str(lab_src)], check=True, capture_output=True)
    subprocess.run([mg, "%s[0]" % str(toned), "-colorspace", "Lab", "-depth", "16",
                    str(lab_new)], check=True, capture_output=True)
    subprocess.run([mg, str(lab_src), str(lab_new), "-channel", "GB",
                    "-compose", "blend", "-define", "compose:args=%g" % (color * 100),
                    "-composite", "+channel", str(mixed)], check=True, capture_output=True)
    # Профиль исходника переносится РУКАМИ. Перевод в Lab и обратно его теряет,
    # а дальше по цепочке magick получает файл без ICC и на `-profile target`
    # уже не конвертирует, а ПРИСВАИВАЕТ: на диск ложится RGB-картинка
    # с наклеенным CMYK-профилем. Ошибки при этом нет ни одной — брак виден
    # только по `identify` готового файла.
    icc = work / ("tprof_%s.icc" % tag)
    got = subprocess.run([mg, "%s[0]" % str(source), str(icc)],
                         capture_output=True, text=True)
    tail = ["-colorspace", "sRGB", "-depth", "16"]
    if got.returncode == 0 and icc.exists() and icc.stat().st_size > 0:
        tail += ["-profile", str(icc)]
    subprocess.run([mg, str(mixed), str(lab_new), "-channel", "R",
                    "-compose", "blend", "-define", "compose:args=%g" % (light * 100),
                    "-composite", "+channel"] + tail + [str(dst)],
                   check=True, capture_output=True)
    # Если у исходника профиль был, а у результата его нет — дальше по цепочке
    # получится тот самый брак. Лучше отказаться от ограничителя, чем отдать
    # в печать такой файл: вызывающая сторона поймает и возьмёт тон как есть.
    had = subprocess.run([mg, "%s[0]" % str(source), "-format", "%[profile:icc]", "info:"],
                         capture_output=True, text=True).stdout.strip()
    now = subprocess.run([mg, "%s[0]" % str(dst), "-format", "%[profile:icc]", "info:"],
                         capture_output=True, text=True).stdout.strip()
    if had and not now:
        raise RuntimeError('the profile "%s" was lost while limiting the tone' % had)
    for f in (lab_src, lab_new, mixed, icc):
        if f.exists():
            f.unlink()
    return dst, "lightness %g, colour %g" % (light, color)


def native_tone_points(cfg, cand, source, src_icc, workdir, tag):
    """Чёрная, серая и белая точки без Adobe. (путь или None, текст)."""
    m = memory_cfg(cfg)
    job = {
        "magick": cfg["magick"], "src": str(source), "workdir": str(workdir),
        "tag": str(tag), "apply": True, "colorspace": cand.probe["cs"],
        "detectPx": int(m.get("detect_px", 2000)),
        "native": m.get("native") or {}, "tone": cfg.get("global_tone") or {},
    }
    return native_color.tone(job)


def note_skin_distrust(cand):
    """
    Отказ от правки кожи из-за ненадёжной маски — предупреждением в отчёт.

    В журнале он и так есть, но журнал читают при разборе сбоя, а отчёт —
    всегда; среди сотни строк строчку «skin — not corrected» легко потерять.
    Пишется РОВНО ОДИН раз на картинку: отказываются оба этапа сразу (и
    вытягивание, и памятные цвета), причина у них общая, и дублировать её
    в отчёте значит удваивать шум.
    """
    ratio, big, small = native_color.last_skin_disagree()
    if ratio <= 0 or getattr(cand, "skin_distrust_noted", False):
        return
    cand.skin_distrust_noted = True
    cand.warnings.append(msg("skin_mask_untrusted", ratio=ratio, big=big, small=small))
    log.warning("        ! %s", say(cand.warnings[-1], LOG_LANG))


def lift_spec(cfg):
    """Этап вытягивания: включён ли и правит ли. Форма как у tone_spec."""
    o = cfg.get("lift") or {}
    on = bool(o.get("enabled", True)) and stage_on(cfg, "lift")
    return {"enabled": on, "apply": on and bool(o.get("apply", True))}


def native_lift(cfg, cand, source, workdir, tag, apply=True):
    """Вытягивание недодержанного человека. (путь или None, текст)."""
    m = memory_cfg(cfg)
    job = {
        "magick": cfg["magick"], "src": str(source), "workdir": str(workdir),
        "tag": str(tag), "apply": bool(apply), "colorspace": cand.probe["cs"],
        "detectPx": int(m.get("detect_px", 2000)),
        "native": m.get("native") or {},
        "lift": cfg.get("lift") or {},
    }
    return native_color.lift(job)


def colorspace_of(cfg, path):
    """Пространство файла одним дешёвым вызовом: CMYK / GRAY / RGB / ..."""
    proc = subprocess.run(
        [cfg["magick"], "identify", "-ping", "-format", "%[colorspace]",
         "%s[0]" % str(path)], capture_output=True, text=True)
    return (proc.stdout or "").strip().upper()


def same_colorspace(cfg, before, after):
    """
    Совпадает ли пространство до и после чужого движка.

    Сторож на класс ошибки, а не на один случай. Core Image, получив
    CMYK-файл, молча отдаёт sRGB: замер на реальном кадре — вход CMYK,
    4 канала, выход sRGB, 3 канала. Пайплайн продолжал считать файл CMYK
    и применял к нему конверсию CMYK -> CMYK: на оттиске это не «немного
    не тот цвет», а нечитаемая каша из дизерных точек. Один файл номера
    ушёл таким в печать.
    """
    a, b = colorspace_of(cfg, before), colorspace_of(cfg, after)
    def family(cs):
        if cs in ("CMYK", "CMY"):
            return "CMYK"
        if cs in ("GRAY", "GREY", "LINEARGRAY"):
            return "GRAY"
        return "RGB"
    return family(a) == family(b), a, b


def measure_mean_rgb(cfg, path):
    """Среднее по каналам — чтобы показать в отчёте, что сделал чужой алгоритм."""
    proc = subprocess.run(
        [cfg["magick"], "%s[0]" % str(path), "-colorspace", "sRGB", "-format",
         "%[fx:int(mean.r*255)]/%[fx:int(mean.g*255)]/%[fx:int(mean.b*255)]", "info:"],
        capture_output=True, text=True)
    return (proc.stdout or "").strip() or "?"


def _parse_shift(raw):
    """«C:21.4:15.2:0.52:0.3:1:C к M» -> словарь одной правки канала.

    Шестое поле — прицел (1) или ограничитель (0). Логи прошлых прогонов его
    не знают, поэтому разбор обязан переживать и старый шестипольный формат:
    по ним сверяются, когда разбираются «почему тогда вышло иначе».
    """
    parts = str(raw).split(":")
    if len(parts) < 6:
        return None
    try:
        aimed = parts[5] in ("0", "1")
        return {"channel": parts[0], "from": float(parts[1]), "to": float(parts[2]),
                "value": float(parts[3]), "bound": float(parts[4]),
                "aim": aimed and parts[5] == "1",
                "label": ":".join(parts[6:] if aimed else parts[5:])}
    except ValueError:
        return None


def describe_finding(f, lang=None):
    """Finding -> a line for the log. The report renders codes itself."""
    lang = lang or LOG_LANG
    title = MEMORY_COLOR_TITLES.get(f.get("color"), f.get("color"))
    if f.get("part"):
        # Кожа меряется по каждому человеку отдельно: среднее по всем лицам
        # сразу лечит плохо — красное лицо усредняется спокойными.
        title = "%s (%s)" % (title, say(f["part"], lang))
    status = f.get("status")
    if f.get("color") == "tone":
        if status == "ok":
            return "overall tone — within norm"
        if status == "would":
            return "overall tone — %s (not applied, apply = false)" % say(f.get("why"), lang)
        return "overall tone — %s" % say(f.get("why"), lang)
    if status == "none":
        return "%s — region not found" % title
    if status == "skipped":
        # Отказ по недоверию к маске. Причина ОБЯЗАНА быть в строке: иначе
        # в журнале остаётся «skin — 0.0% кадра», и это читается как «кожи
        # нет», а не как «кожа есть, но размечена ненадёжно».
        return "%s — not corrected: %s" % (title, say(f.get("why"), lang))
    if status == "small":
        return "%s — %s, not correcting" % (title, say(f.get("why"), lang))
    if status == "unlike":
        # Безадобный движок отсеивает область ещё до замера — по ярлыкам
        # сюжета от Vision. Красок в этом случае нет, и печатать «по числам»
        # нечего: причина уже записана словами.
        if not f.get("before"):
            return "%s — %s: not looking" % (title, say(f.get("why"), lang) or "region not confirmed")
        return ("%s — a region exists (%.1f%% of the frame, %s), but by the numbers "
                "it is not %s: left alone"
                % (title, f.get("area") or 0, f.get("before"), title))
    if status == "error":
        return "%s — %s" % (title, say(f.get("why"), lang) or "failed")
    if status == "ok":
        return "%s — %.1f%% of the frame, %s: within norm" % (title, f.get("area") or 0,
                                                              f.get("before"))
    shifts = [s for s in (_parse_shift(x) for x in (f.get("shift") or "").split("|")) if s]
    moves = "; ".join(
        "%s = %s, %s → %s %.0f → %.0f%%"
        % (s["label"], _fmt_num(s["value"]),
           ("target %s" % _fmt_num(s["bound"])) if s.get("aim")
           else ("norm %s %s" % ("at most" if s["value"] > s["bound"] else "at least",
                                 _fmt_num(s["bound"]))),
           s["channel"], s["from"], s["to"])
        for s in shifts)
    head = "%s — %.1f%% of the frame" % (title, f.get("area") or 0)
    if status == "would":
        return "%s: %s (not applied, apply = false); before %s, would be %s" % (
            head, moves, f.get("before"), f.get("after"))
    return "%s: %s; before %s, after %s" % (head, moves, f.get("before"), f.get("after"))


def _fmt_num(v):
    if v is None:
        return "?"
    return ("%.2f" % v).rstrip("0").rstrip(".") if abs(v) < 10 else "%.0f" % v


def file_rgb_icc(cfg, source, src_icc, workdir, tag):
    """
    Профиль, в котором записаны числа файла.

    Есть встроенный — вынимаем его: считать зашитый в файл AdobeRGB за sRGB
    значит промахнуться по краске на несколько процентов ещё до всякой правки.
    Нет — берём тот, что назначен по умолчанию (src_icc), он для того и есть.
    """
    if src_icc:
        return str(src_icc)
    dst = Path(workdir) / ("srcicc_%s.icc" % tag)
    try:
        subprocess.run([cfg["magick"], "%s[0]" % str(source), str(dst)],
                       capture_output=True, text=True, check=True)
    except Exception:
        return str(cfg["assume_rgb_icc"])
    return str(dst) if dst.exists() and dst.stat().st_size > 0 else str(cfg["assume_rgb_icc"])


def run_native_color_correct(cfg, cand, source, src_icc, workdir, tag, apply_changes):
    """
    То же самое без Adobe: маски от Vision, арифметика и кривые от magick.

    Задание и ответ той же формы, что у color_correct.jsx, — отчёт, лог
    и манифест разницы не замечают.
    """
    dst = Path(workdir) / ("corr_%s.tif" % tag)
    m = memory_cfg(cfg)
    job = {
        "magick": cfg["magick"],
        "src": str(source),
        "dst": str(dst),
        "workdir": str(workdir),
        "tag": str(tag),
        "apply": bool(apply_changes),
        "colorspace": cand.probe["cs"],
        "detectPx": int(m.get("detect_px", 2000)),
        "maxShift": float(m.get("max_shift_percent", 12)),
        "strength": float(m.get("strength", 0.7)),
        "targetIcc": str(target_icc_for(cfg, cand.probe["cs"])),
        "rgbIcc": file_rgb_icc(cfg, source, src_icc, workdir, tag),
        "intent": cfg["rendering_intent"],
        "bpc": bool(cfg.get("black_point_compensation")),
        "colors": memory_color_specs(cfg),
        "native": m.get("native") or {},
    }
    r = native_color.correct(job)
    note_skin_distrust(cand)
    findings = r.get("findings") or []
    for w in (r.get("warnings") or []):
        cand.warnings.append(w)
        log.warning("        ! %s", w)
    if r.get("skipped"):
        log.info("        colour      : %s", say(r["skipped"], LOG_LANG))
    if not r.get("changed"):
        return None, findings
    if not dst.exists():
        raise RuntimeError("magick did not write the file %s" % dst.name)
    return dst, findings


def run_color_correct(cfg, cand, source, src_icc, workdir, tag, apply_changes):
    """
    Гоняет один файл через color_correct.jsx.

    Возвращает (путь к исправленному TIFF или None, список находок). Файл
    пишется, только если правка действительно понадобилась: тогда дальше по
    цепочке идёт он, иначе — исходник, и лишней перезаписи не будет.

    Ошибку поднимает исключением — решает вызывающий. Photoshop на этой
    работе иногда падает целиком (ловилось на colorRange в CMYK), и прогон
    из-за одной картинки останавливаться не должен.
    """
    dst = Path(workdir) / ("corr_%s.tif" % tag)
    target_icc = target_icc_for(cfg, cand.probe["cs"])
    target_desc = icc_description(cfg, target_icc)
    if not target_desc:
        raise RuntimeError('could not read the profile name from "%s"' % target_icc)
    rgb_desc = icc_description(cfg, cfg["assume_rgb_icc"])
    if not rgb_desc:
        raise RuntimeError('could not read the profile name from "%s"'
                           % cfg["assume_rgb_icc"])

    src_desc = ""
    if src_icc:
        src_desc = icc_description(cfg, src_icc)
        if not src_desc:
            raise RuntimeError('could not read the profile name from "%s"' % src_icc)

    source, renamed = photoshop_ready_source(cfg, source, workdir, tag)
    if renamed:
        cand.warnings.append(renamed)
        log.warning("        ! %s", renamed)

    m = memory_cfg(cfg)
    job = {
        "src": str(source),
        "dst": str(dst),
        "apply": bool(apply_changes),
        "detectPx": int(m.get("detect_px", 2000)),
        "maxShift": float(m.get("max_shift_percent", 12)),
        "strength": float(m.get("strength", 0.7)),
        "targetProfile": target_desc,
        "rgbProfile": rgb_desc,
        "srcProfile": src_desc,
        "intent": ps_intent(cfg),
        "blackPointCompensation": bool(cfg.get("black_point_compensation")),
        "colors": memory_color_specs(cfg),
        "tone": (dict(tone_spec(cfg), enabled=False)
                 if tone_engine(cfg) == "coreimage" else tone_spec(cfg)),
    }
    report = run_adobe_jsx(cfg, photoshop_app_id(cfg), JSX_DIR / "color_correct.jsx",
                           {"jobs": [job]}, PS_PARAMS_PATH,
                           Path(workdir) / ("corr_report_%s.json" % tag),
                           "цветокоррекция памятных цветов")

    results = report.get("results") or []
    r = results[0] if results else None
    if r is None or not r.get("ok"):
        raise RuntimeError((r or {}).get("error") or "Photoshop returned no result")

    findings = r.get("findings") or []
    if r.get("timings"):
        log.debug("        colour      : timings %s", r["timings"])
        globals()["_last_timings"] = r["timings"]
    for w in (r.get("warnings") or "").split("|"):
        if w.strip():
            cand.warnings.append(w.strip())
            log.warning("        ! %s", w.strip())
    # Grayscale и прочие режимы, где памятных цветов не ищут вовсе.
    if r.get("skipped"):
        log.info("        colour      : %s", say(r["skipped"], LOG_LANG))
    if not r.get("changed"):
        return None, findings
    if not dst.exists():
        raise RuntimeError("Photoshop did not write the file %s" % dst.name)
    return dst, findings


def color_correct_candidate(cfg, cand, source, src_icc, workdir, tag, apply_changes):
    """
    Обёртка над run_color_correct: пишет находки в лог и в кандидата.

    Сбой цветокоррекции не должен ронять обработку файла — картинка нужна
    в CMYK и правильного разрешения в любом случае, а памятные цвета
    поправят руками, если Photoshop не смог.
    """
    native = memory_engine(cfg) == "native"
    log_stage("memory colours (%s)" % ("Vision + magick" if native else "Photoshop"),
              ui="memory colours")
    runner = run_native_color_correct if native else run_color_correct
    try:
        out, findings = runner(cfg, cand, source, src_icc, workdir,
                               tag, apply_changes)
    except Exception as exc:
        # Исчерпанный рабочий диск — не повод терять картинку: Photoshop
        # отдаёт scratch только при выходе, так что закрываем его и пробуем
        # ещё раз. Один раз: если и после чистого старта места нет, значит
        # его нет на диске, и повторять бессмысленно.
        if not native and is_scratch_full(exc):
            restart_photoshop(cfg, "рабочий диск исчерпан на %s" % cand.path.name)
            try:
                out, findings = runner(cfg, cand, source, src_icc, workdir,
                                       tag, apply_changes)
            except Exception as exc2:
                note = ("цветокоррекция памятных цветов не выполнена и после "
                        "перезапуска Photoshop (%s)" % exc2)
                cand.warnings.append(note)
                log.error("        ERROR       : %s", note)
                return None
        else:
            note = "memory-colour correction failed (%s)" % exc
            cand.warnings.append(note)
            log.error("        ERROR       : %s", note)
            return None
    cand.color_findings = findings
    for f in findings:
        log.info("        colour      : %s", describe_finding(f))
    if out is not None:
        cand.correct_source = out
        cand.color_corrected = True
    return out


# --------------------------------------------------------------------------- #
#  Обработка
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
#  Куда сохранять сам документ InDesign
# --------------------------------------------------------------------------- #
#
# Боевой прогон по умолчанию не трогает исходный .indd: он сохраняет результат
# рядом под именем «<имя>_CMYK.indd», а оригинал остаётся со связями на исходные
# (как правило, RGB) картинки — из него потом собирают PDF для экранов.
# Повторный прогон уже готового «..._CMYK.indd» новых копий не плодит:
# суффикс на месте, значит документ просто сохраняется сам в себя.


def doc_output_opts(cfg):
    return cfg.get("output_document") or {}


def image_suffix(cfg):
    """
    Суффикс имени обработанной картинки, когда исходник остаётся рядом.

    По умолчанию «_cmyk». Зачем настройка: имя вида `кадр_cmyk_3.jpg` говорит
    только «третий по счёту файл с таким именем» и ничего — о том, чем он
    сделан. Стоило один раз переложить набор одного прогона в сторону, как
    следующий прогон занял освободившийся номер, и два разных движка стали
    неотличимы по имени. Суффикс вида «_ps» / «_cli» снимает вопрос:
    происхождение видно в папке связей, без заглядывания в лог.
    """
    value = str(doc_output_opts(cfg).get("image_suffix") or "auto").strip()
    if value != "auto":
        return value
    # «auto»: суффикс не должен ВРАТЬ. Прогон, который только поправил цвет,
    # выдавал файлы «_cmyk», хотя цветоделения в цепочке не было вовсе —
    # по имени выходило, что файл готов к печати, а он в RGB. Поэтому имя
    # говорит о том, что с файлом действительно сделали.
    table = doc_output_opts(cfg).get("image_suffixes") or {}

    def pick(name, fallback):
        return str(table.get(name) or fallback).strip()

    if stage_on(cfg, "convert"):
        return (pick("to_rgb", "_rgb") if color_direction(cfg) == "to_rgb"
                else pick("to_cmyk", "_cmyk"))
    if stage_on(cfg, "tone") or stage_on(cfg, "memory"):
        return pick("corrected", "_corrected")
    return pick("other", "_prepress")


def doc_suffix(cfg):
    return doc_output_opts(cfg).get("suffix") or "_CMYK"


def save_as_new_document(cfg):
    return bool(doc_output_opts(cfg).get("save_as_new", True))


def document_save_path(doc_real, cfg):
    """
    Куда сохранять документ: путь нового файла или None, если сохраняем в себя.

    None возвращается в двух случаях: сохранение «в новый файл» выключено
    в настройках или суффикс в имени уже есть (значит, это и есть тот самый
    файл, который создал прошлый прогон).
    """
    if not save_as_new_document(cfg):
        return None
    doc_real = Path(doc_real)
    suffix = doc_suffix(cfg)
    if doc_real.stem.lower().endswith(suffix.lower()):
        return None
    return doc_real.with_name(doc_real.stem + suffix + doc_real.suffix)


def keep_originals_in_place(cfg):
    """
    Оставлять ли исходные картинки там, где они лежат.

    Иначе исходный .indd, который мы намеренно не трогаем, остался бы со
    связями на файлы, уехавшие в originals/ — то есть битым, и вся затея
    «сохранить версию с RGB-картинками» потеряла бы смысл.
    """
    return str(cfg.get("originals_mode", "move")).lower() == "keep"


def unique_path(path):
    """Свободный путь: добавляет _2, _3 ... если файл уже занят."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while True:
        cand = parent / ("%s_%d%s" % (stem, n, suffix))
        if not cand.exists():
            return cand
        n += 1


def originals_target(cand, doc_dir, originals_dir):
    """
    Куда положить оригинал. Если файл лежал внутри папки документа —
    структура подпапок сохраняется (originals/Links/foo.jpg).
    """
    try:
        rel = cand.path.relative_to(doc_dir)
        dest = originals_dir / rel
    except ValueError:
        safe = re.sub(r"[^\w.-]+", "_", str(cand.path.parent)).strip("_")[-60:]
        dest = originals_dir / "_external" / safe / cand.path.name
    return unique_path(dest)


# --------------------------------------------------------------------------- #
#  В каком формате писать результат
# --------------------------------------------------------------------------- #

OUTPUT_FORMATS = ("jpeg", "tiff")
_FORMAT_EXT = {"jpeg": ".jpg", "tiff": ".tif"}


def output_format(cfg):
    fmt = str(cfg.get("output_format", "jpeg")).lower()
    return fmt if fmt in OUTPUT_FORMATS else "jpeg"


def jpeg_quality(cfg):
    try:
        q = int(round(float(cfg.get("jpeg_quality", 100))))
    except (TypeError, ValueError):
        q = 100
    return max(1, min(100, q))


# Форматы, которые кладут в вёрстку ради качества: раз исходник без потерь,
# то и результат должен остаться без потерь. Список совпадает с
# SPOT_RASTER_FORMATS по составу, но смысл у него другой, поэтому свой.
LOSSLESS_SOURCE_FORMATS = (".tif", ".tiff", ".psd", ".psb")


def output_format_for(cfg, cand):
    """
    Формат для конкретной картинки и причина, если он не тот, что в настройках.

    Два случая, когда JPEG из настроек не годится:

    1. В файле есть альфа-канал. JPEG прозрачность не хранит, и вместо
       прозрачных мест картинка получила бы сплошную заливку — в вёрстке это
       вылезает подложкой под объектом, а заметить можно уже в тираже.
    2. Исходник сам без потерь — TIFF или PSD. Такие форматы кладут в вёрстку
       не по случайности, а ради качества; отдать назад JPEG значило бы
       обесценить решение, принятое до нас.

    В обоих случаях пишем TIFF, а причина уходит в лог: молчаливое
    расхождение с настройкой хуже, чем лишняя строка.
    """
    fmt = output_format(cfg)
    if fmt == "jpeg":
        if (cand.probe or {}).get("has_alpha"):
            return "tiff", "the file has an alpha channel, JPEG cannot store it"
        ext = cand.path.suffix.lower()
        if ext in LOSSLESS_SOURCE_FORMATS:
            return "tiff", "source %s, lossless format" % ext
    return fmt, None


def output_ext(cfg, cand=None):
    if cand is None:
        return _FORMAT_EXT[output_format(cfg)]
    return _FORMAT_EXT[output_format_for(cfg, cand)[0]]


def describe_output(cfg):
    """Строка «во что пишем» для шапки лога и отчёта."""
    if output_format(cfg) == "jpeg":
        return "JPEG, quality %d, no chroma subsampling" % jpeg_quality(cfg)
    return "TIFF %s bit, compression %s" % (cfg["tiff_depth"], cfg["tiff_compression"])


def target_width(cand):
    """
    Целевая ширина в пикселях. Одна на всех, чтобы ресемплинг и плотность
    итогового файла не разошлись, в каком бы вызове magick он ни делался.

    Считается от ОБРЕЗАННОГО куска, если обрезка была: коэффициент ресемплинга
    задаётся effective PPI и от обрезки не меняется, а вот пиксели — меняются.
    """
    base = cand.crop["box"][2] if (cand.crop and cand.crop["cropped"]) else cand.probe["width"]
    return max(1, int(round(base * cand.resize_factor)))


def resample_args(cfg, cand):
    """Ключи magick для ресемплинга и человеческое описание к ним."""
    if cand.resample_kind == "upsample":
        # На увеличении Lanczos склонен «звенеть» по контурам, поэтому
        # для апскейла фильтр отдельный (по умолчанию Mitchell).
        filt = cfg.get("upscale_filter") or cfg["resize_filter"]
        what = "upscale"
    elif cand.resample_kind == "ai_upscale":
        # После модели картинка в 4 раза больше нужного — ужимаем вниз.
        filt = cfg["resize_filter"]
        what = "shrink after AI upscale"
    else:
        filt = cfg["resize_filter"]
        what = "downscale"
    new_w = target_width(cand)
    return (["-filter", filt, "-resize", "%dx" % new_w],
            "%s %s -> width %d px (%.4f%%)"
            % (what, filt, new_w, cand.resize_factor * 100))


def build_resize_command(cfg, cand, out_path, source):
    """
    Отдельный вызов magick перед Photoshop: ресемплинг С ЗАПАСОМ.

    Зачем вообще: Photoshop должен открывать картинку близкого к целевому
    размера, а не исходную. На кадре с полнокадровой камеры это разница между
    50 и 3 мегапикселями — а значит и между секундами и десятками секунд
    на открытие, маски, кривые и запись.

    Почему с запасом, а не сразу до цели: правка тянет каналы, а вместе с ними
    и шум. Если тянуть уже по финальному размеру, усреднять растянутое зерно
    нечем — на бледном небе шум удваивался (2.2 -> 4.1 по стандартному
    отклонению, видно глазом). Оставляя запас (oversample), мы даём финальному
    ужатию в build_command сгладить то, что подняла правка.

    Глубину намеренно не задаём: 16-битный исходник должен дойти до
    цветоделения шестнадцатибитным, как и при обработке одним вызовом.
    """
    factor = float(crop_cfg(cfg).get("correction_oversample", 2.0))
    want = int(round(target_width(cand) * max(1.0, factor)))
    # Выше исходника не поднимаемся: тянуть пиксели ради запаса бессмысленно.
    src_w = (cand.crop["box"][2] if (cand.crop and cand.crop["cropped"])
             else cand.probe["width"])
    if cand.resample_kind == "ai_upscale":
        src_w = int(src_w * 4)          # после модели пикселей вчетверо больше
    new_w = max(1, min(want, src_w))
    filt = (cfg.get("upscale_filter") or cfg["resize_filter"]) \
        if cand.resample_kind == "upsample" else cfg["resize_filter"]
    cmd = [cfg["magick"], "%s[0]" % str(source),
           "-filter", filt, "-resize", "%dx" % new_w,
           "-compress", cfg["tiff_compression"], str(out_path)]
    return cmd, ("resample with headroom -> %d px (target %d, headroom x%g)"
                 % (new_w, target_width(cand), factor))


def build_command(cfg, cand, out_path, src_icc, new_density, source=None):
    """
    Собирает вызов magick. Порядок операций важен: ресайз -> цвет -> резкость -> запись.

    source — чем подменить исходник (после ИИ-апскейла это увеличенный PNG).
    Целевая ширина считается всегда от ОРИГИНАЛЬНЫХ пикселей, поэтому промежуточный
    размер значения не имеет: -resize задаёт абсолютное число пикселей.
    """
    cmd = [cfg["magick"], "%s[0]" % str(source or cand.path)]
    steps = []

    # Ресемплинг делается ВСЕГДА здесь — даже если перед Photoshop картинку
    # уже ужали с запасом: именно это финальное ужатие и сглаживает шум,
    # который подняла правка.
    if cand.needs_resample or cand.resample_done:
        args, note = resample_args(cfg, cand)
        cmd += args
        steps.append(note + ("" if not cand.resample_done
                             else " (from the headroom kept before correction)"))

    if cand.needs_color:
        # Присваиваем исходный профиль, только если своего в файле нет.
        if src_icc:
            cmd += ["-profile", str(src_icc)]
            steps.append('source profile assumed: "%s"' % Path(src_icc).name)
        cmd += ["-intent", cfg["rendering_intent"]]
        if cfg.get("black_point_compensation"):
            cmd += ["-black-point-compensation"]
        if color_direction(cfg) == "to_rgb":
            dst_icc = target_rgb_icc(cfg)
            what = "convert to RGB"
        else:
            dst_icc = target_icc_for(cfg, cand.probe["cs"])
            what = "separation"
        cmd += ["-profile", str(dst_icc)]
        steps.append('%s -> "%s" (%s%s)'
                     % (what, Path(dst_icc).name, cfg["rendering_intent"],
                        " + BPC" if cfg.get("black_point_compensation") else ""))

    sh_args, sh_note = sharpen_args(cfg, {
        "resize_factor": cand.resize_factor,
        "colorspace": cand.probe["cs"],
        "path": str(cand.path),
    })
    cmd += sh_args
    steps.append("sharpening: %s" % sh_note)

    # Формат берём из расширения, которое уже выбрано для этой картинки
    # (см. output_format_for): так команда и имя файла не могут разойтись.
    cmd += ["-density", "%.4f" % new_density, "-units", "PixelsPerInch"]
    if Path(out_path).suffix.lower() in (".jpg", ".jpeg"):
        # JPEG бывает только 8-битным, что бы ни стояло в tiff_depth.
        quality = jpeg_quality(cfg)
        cmd += ["-depth", "8", "-quality", str(quality)]
        # Без этого magick прореживает цветовые каналы вдвое (4:2:0). На
        # фотографии почти незаметно, а на контрастной границе и на мелком
        # тексте внутри картинки — размытая кайма, которую печать покажет.
        cmd += ["-sampling-factor", "1x1"]
        steps.append("write JPEG quality %d, no chroma subsampling, "
                     "density %.2f ppi" % (quality, new_density))
    else:
        cmd += ["-depth", str(cfg["tiff_depth"])]
        cmd += ["-compress", cfg["tiff_compression"]]
        steps.append("write TIFF %d bit, compression %s, density %.2f ppi"
                     % (cfg["tiff_depth"], cfg["tiff_compression"], new_density))

    cmd += [str(out_path)]
    return cmd, steps


def fmt_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def describe_probe(info):
    """
    Файл одной строкой. Кроме размера и профиля показываются СЛОИ и лишние
    КАНАЛЫ: на сухом прогоне по папке это единственный способ увидеть, что
    PSD многослойный (он будет сведён) и что в файле есть альфа или
    плашечный канал. По одному «CMYK, 8 бит» этого не понять, а решение
    печатника от этого зависит.
    """
    extra = []
    scenes = info.get("scenes") or 0
    if scenes and int(scenes) > 1:
        extra.append("layers %d" % int(scenes))
    chan = int(info.get("channel_count") or 0)
    base = {"CMYK": 4, "RGB": 3, "GRAY": 1}.get(info.get("cs"), 0)
    if chan and base and chan > base:
        extra.append("channels %d (+%d)" % (chan, chan - base))
    return ('%dx%d px, %s%s, %.1f ppi (in file), %d bit, %s, %s%s'
            % (info["width"], info["height"], info["cs"],
               ' / "%s"' % info["icc_desc"] if info.get("icc_desc") else " no ICC",
               info["ppi_x"], info["depth"], info.get("format", "?"),
               fmt_size(info.get("filesize")),
               (", " + ", ".join(extra)) if extra else ""))


# --------------------------------------------------------------------------- #
#  Логирование
# --------------------------------------------------------------------------- #

def setup_logging(log_file, verbose):
    log.setLevel(logging.DEBUG)
    log.handlers[:] = []

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(ch)


# --------------------------------------------------------------------------- #
#  События для приложения
# --------------------------------------------------------------------------- #
#
# Приложению нужен прогресс, а не человекочитаемый лог. Дроплет разбирает
# строки «этап : …» регулярками — так делать больше нельзя: стоит поправить
# формулировку в логе, и индикатор снаружи молча ломается. Поэтому событие
# печатается ОТДЕЛЬНОЙ строкой JSON в stdout, а лог остаётся тем, чем был —
# человеческим рассказом для разбора ошибок.
#
# Одно событие — одна строка, без вложенных переводов строки: приложение
# читает stdout построчно и не должно склеивать куски.
EVENTS_ON = False

# Язык журнала. Английский всегда и намеренно: журнал читают при разборе
# сбоя — свой, чужой, через полгода, — и один язык диагностики дешевле двух.
# Отчёт от этого не зависит: он рисуется из манифеста, где лежат коды,
# и показывается на языке из report_lang.
LOG_LANG = "en"


def emit(kind, **fields):
    """Событие в stdout, если прогон запущен с --json-events."""
    if not EVENTS_ON:
        return
    payload = {"event": kind}
    payload.update(fields)
    try:
        line = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        line = json.dumps({"event": kind, "error": "не сериализуется"},
                          ensure_ascii=False)
    sys.stdout.write(line.replace("\n", " ") + "\n")
    sys.stdout.flush()


def log_stage(what, ui=None, **fields):
    """
    Что делается прямо сейчас — строка для индикатора и событие для приложения.

    Пишется ПЕРЕД началом операции, а не после неё. Это существенно: долгие
    этапы до сих пор не печатали ничего, пока не закончатся, и ИИ-апскейл
    (минуты на картинку) снаружи выглядел замершим прогоном. Дроплет читает
    эти строки из вывода, см. runInBackground в Prepress.applescript.

    У строки два читателя с разными нуждами. В ЖУРНАЛЕ нужен инструмент:
    «Vision + magick» или «Core Image» отвечают на вопрос, чем именно сделан
    кадр, — без этого разбор сбоя превращается в гадание. В ОКНЕ приложения
    это шум: человек смотрит, далеко ли до конца, а не какой библиотекой
    посчитана маска. Поэтому `ui` — короткая подпись для окна; журнал
    получает полную в любом случае.
    """
    log.info("        stage       : %s", what)
    emit("stage", what=(ui or what), **fields)


def open_report(cfg, path):
    """Открывает готовый отчёт в приложении по умолчанию для .md."""
    if not cfg.get("open_report_on_finish"):
        return
    try:
        proc = subprocess.run(["open", str(path)], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            log.warning("Could not open the report: %s", proc.stderr.strip())
    except Exception as exc:
        log.warning("Could not open the report: %s", exc)


def notify(cfg, title, text):
    if not cfg.get("notify_on_finish"):
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title %s' % (_as_quote(text), _as_quote(title))],
            capture_output=True, text=True, timeout=10)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Основной сценарий
# --------------------------------------------------------------------------- #

def process_document(cfg, doc_path, args, files=None):
    """
    Прогон одного макета — или одной пачки файлов, если files заданы.

    Оба входа идут ОДНИМ путём: список файлов приводится к той же форме,
    что отдаёт collect_links.jsx, и дальше всё общее — разбор, классификация,
    цикл обработки. Отдельная ветка «для папки» разошлась бы с пайплайном
    на третьей мелочи; это уже проверялось и стоило дня.
    """
    workdir = Path(tempfile.mkdtemp(prefix="prepress_"))
    started = _dt.datetime.now()
    on_files = files is not None
    stamp = started.strftime("%Y%m%d_%H%M%S")

    # 1. Откуда список картинок ---------------------------------------------
    cfg = dict(cfg, __on_files=on_files)
    pasteboard = None
    if on_files:
        data = {"links": files_as_links(cfg, files), "linkCount": len(files)}
        src_dir = Path(files[0]).parent if files else Path.cwd()
        doc_dir = src_dir
        # Псевдо-«документ»: нужен только чтобы назвать лог, отчёт и манифест.
        doc_real = src_dir / (src_dir.name + ".папка")
    else:
        # Поля чистим ДО сбора связей: иначе забытые кадры уже попали
        # в список, и дальше их не отличить от настоящих. Журнала ещё нет
        # (он заводится по имени документа, а имя придёт из collect_links),
        # поэтому отчёт откладываем и печатаем ниже.
        if stage_on(cfg, "pasteboard"):
            log_stage("clearing the pasteboard")
            pasteboard = clean_pasteboard(cfg, doc_path, workdir,
                                          apply=not args.dry_run)
        log_stage("reading links from InDesign")
        data = collect_links(cfg, doc_path, workdir)
        doc_real = Path(data.get("documentPath") or doc_path or "")
        if not str(doc_real) or str(doc_real) in (".", ""):
            raise RuntimeError(
                "Не удалось определить путь документа на диске. "
                "Сохраните документ в InDesign (File > Save) и запустите заново.")
        doc_dir = doc_real.parent

    logs_dir = doc_dir / cfg["logs_dir_name"]
    logs_dir.mkdir(parents=True, exist_ok=True)
    base = "%s_%s" % (doc_real.stem, stamp)
    setup_logging(logs_dir / (base + ".log"), args.verbose)

    log.info("=" * 78)
    log.info("Prepress image processing")
    if on_files:
        log.info("Folder        : %s", doc_dir)
        log.info("Files         : %s", data.get("linkCount"))
    else:
        log.info("Document      : %s", doc_real)
        log.info("InDesign      : %s", data.get("appVersion"))
        log.info("Links total   : %s", data.get("linkCount"))
    log.info("Output        : %s", describe_files_output(cfg, doc_dir))
    if not on_files and pasteboard is not None:
        gone = pasteboard.get("removed") or []
        if gone:
            log.info("Pasteboard    : %s %d image(s): %s",
                     "removed" if pasteboard.get("applied") else "would remove",
                     len(gone), ", ".join(gone[:8])
                     + (", …" if len(gone) > 8 else ""))
        else:
            log.info("Pasteboard    : clean")
        for bad in (pasteboard.get("failed") or []):
            log.warning("Pasteboard    : could not remove %s", bad)
    log.info("Target ICC    : %s", cfg["target_icc"])
    if cfg.get("convert_grayscale_to_cmyk"):
        log.info("ICC for grey  : %s", target_icc_for(cfg, "GRAY"))
    else:
        log.info("ICC for grey  : not used (grey stays K-only)")
    log.info("Resampling    : > %s -> %s;  %s..%s untouched;  %s..%s -> %s;  < %s report only",
             cfg["max_effective_ppi"], cfg["target_effective_ppi"],
             cfg.get("upscale_below_ppi"), cfg["max_effective_ppi"],
             cfg.get("upscale_min_ppi"), cfg.get("upscale_below_ppi"),
             cfg["target_effective_ppi"], cfg.get("upscale_min_ppi"))
    if stage_on(cfg, "bleed"):
        log.info("Bleed required: at least %s mm past trim (%s)",
                 cfg.get("bleed_required_mm"), cfg.get("bleed_scope"))
    log.info("Text in links : %s",
             "check disabled (text_in_links.enabled = false)" if not check_text(cfg)
             else "check + outline (Illustrator)" if want_outlines(cfg)
             else "check and warn only (convert_to_outlines = false)")
    log.info("Spots (Pantone): %s",
             "check disabled (spot_colors.enabled = false)" if not check_spots(cfg)
             else "check + convert to CMYK (Illustrator / Photoshop)"
             if want_spot_convert(cfg)
             else "только проверка и предупреждение (convert_to_cmyk = false)")
    log.info("Memory colours: %s", describe_memory_colors(cfg))
    log.info("Output format : %s", describe_output(cfg))
    # Сохранять ли результат в новый файл — решаем до всякой обработки:
    # от этого зависит и судьба исходных картинок.
    save_as = None if on_files else document_save_path(doc_real, cfg)
    if save_as is not None and not keep_originals_in_place(cfg):
        # Исходный .indd остаётся со связями на файлы, уехавшие в originals/,
        # то есть битым. Раньше скрипт на этом молча включал keep — и выбранный
        # перенос оригиналов просто не исполнялся. Так делать нельзя: настройки
        # для того и существуют, чтобы разные издания делались по-разному.
        # Решение за человеком, а наше дело — сказать вслух, что будет и как
        # это откатить. Откат дешёвый: вернуть файлы из originals/ на место.
        log.warning("originals go to %s/ while the layout is saved to a new file: "
                    "the SOURCE document will be left with links pointing into "
                    "%s/. To make it whole again, move those files back.",
                    cfg["originals_dir_name"], cfg["originals_dir_name"])

    if on_files:
        pass          # что с исходниками — уже сказано строкой «Результат»
    elif keep_originals_in_place(cfg):
        log.info("Originals     : kept in place — they are their own backup")
    else:
        log.info("Originals     : %s -> %s/",
                 "moved" if cfg["originals_mode"] == "move" else "copied",
                 cfg["originals_dir_name"])
    if save_as is not None:
        log.info("Document      : result -> %s", save_as.name)
        log.info("                source %s stays unchanged", doc_real.name)
        if save_as.exists():
            log.warning("WARNING: %s already exists and will be overwritten.", save_as.name)
    elif save_as_new_document(cfg) and not on_files:
        log.info("Document      : the name already has %s — saving onto itself",
                 doc_suffix(cfg))
    log.info("Mode          : %s", "ANALYSIS ONLY (--dry-run)" if args.dry_run else "live")
    log.info("Engine        : %s", describe_engine(cfg))
    log.info("Chain         : %s", describe_pipeline(cfg.get("__stages") or STAGE_ORDER))
    emit("run_start", document=str(doc_real), onFiles=bool(on_files),
         stages=list(cfg.get("__stages") or STAGE_ORDER),
         engine=pipeline_engine(cfg), dryRun=bool(args.dry_run))
    log.info("Separation by : %s", "Adobe Photoshop"
             if color_engine(cfg) == "photoshop" else "magick (command line)")
    log.info("Log           : %s", logs_dir / (base + ".log"))
    log.info("=" * 78)

    # Проверяем один раз здесь, а не на каждом файле: если Photoshop не стоит,
    # человек должен узнать это до прогона, а не из череды одинаковых ошибок.
    if color_engine(cfg) == "photoshop" and app_path(photoshop_app_id(cfg)) is None:
        log.error("WARNING: separation in Photoshop is selected, but it was not found (%s). "
                  "Цвет будет поделён magick — результат корректен, но движок "
                  "не тот, что выбран в настройках.", photoshop_app_id(cfg))
        cfg["color_engine"] = "magick"

    # Памятные цвета проверяем по тому движку, который для них и выбран:
    # варианту с Photoshop нужен Photoshop, безадобному — помощник Vision.
    # Выключаем сразу, а не по картинке за раз: человек должен узнать об этом
    # до прогона, а не из череды одинаковых ошибок.
    if check_memory_colors(cfg):
        if memory_engine(cfg) == "native":
            ready, why = native_color.available()
            if not ready:
                log.error("WARNING: memory colours without Adobe are on, but %s — "
                          "the check was skipped.", why)
                cfg = dict(cfg, memory_colors=dict(memory_cfg(cfg), enabled=False))
        elif app_path(photoshop_app_id(cfg)) is None:
            log.error("WARNING: memory colours are on, but Photoshop was not found (%s) — "
                      "the check was skipped.", photoshop_app_id(cfg))
            cfg = dict(cfg, memory_colors=dict(memory_cfg(cfg), enabled=False))

    # Общий тон Core Image идёт своим процессом — Adobe ему не нужен, но
    # помощник должен быть собран.
    if tone_engine(cfg) == "coreimage" and not CORE_IMAGE_TOOL.exists():
        log.error("WARNING: Core Image tone is selected, but the helper is not built "
                  "(%s) — run ./setup.sh. The stage was skipped.",
                  CORE_IMAGE_TOOL.name)
        cfg = dict(cfg, global_tone=dict(cfg.get("global_tone") or {}, enabled=False))

    if data.get("documentModified") and not args.dry_run and not args.no_save:
        log.warning("WARNING: the document has unsaved changes. "
                    "The script will Save at the end — they will be written.")
    if (data.get("openCopies") or 1) > 1:
        log.warning("WARNING: this file is open in InDesign in %d copies. "
                    "Changes will reach only one of them — close the extra windows.",
                    data["openCopies"])

    target_desc = icc_description(cfg, cfg["target_icc"])
    log.info('Target profile name inside the ICC: "%s"', target_desc)

    # 2. Группировка и отбор ------------------------------------------------
    candidates, skipped = group_links(data.get("links", []), cfg)
    log.info("")
    log.info("Unique raster files: %d", len(candidates))
    if skipped:
        log.info("Not considered (%d):", len(skipped))
        for name, why in skipped:
            log.info("    - %s — %s", name, why)
        # Массово отсутствующие связи — почти всегда не свойство макета,
        # а его состояние: документ остался открыт с несохранённым
        # переподключением от прошлого прогона, а файлы, на которые он теперь
        # ссылается, уже убраны. Прогон в таком виде отрабатывает «успешно»
        # и не делает ничего — на 111 связях из 112 это ловилось только
        # чтением лога. Поэтому говорим громко.
        missing = [n for n, w in skipped if "MISSING" in w]
        if missing and len(missing) >= max(3, len(data.get("links", [])) // 4):
            log.error("")
            log.error("WARNING: %d of %d links have no file on disk. "
                      "There is almost nothing to process.", len(missing),
                      len(data.get("links", [])))
            log.error("Common cause: the document is open in InDesign with "
                      "an unsaved relink from a previous run. "
                      "Close it without saving and try again.")

    todo, compliant = [], []
    log.info("")
    log.info("--- Analysis ---")
    for cand in candidates:
        try:
            cand.probe = probe_image(cfg, cand.path)
        except Exception as exc:
            cand.skip_reason = "не удалось прочитать файл: %s" % exc
            log.error("!! %s — %s", cand.path.name, cand.skip_reason)
            skipped.append((cand.path.name, cand.skip_reason))
            continue
        if check_spots(cfg) and cand.path.suffix.lower() in SPOT_RASTER_FORMATS:
            cand.spot_info = scan_raster_spots(cand.path)
        matched = classify(cand, cfg, target_desc, doc_dir)
        if cand.skip_reason:
            log.warning("!! %s — %s", cand.path.name, cand.skip_reason)
            skipped.append((cand.path.name, cand.skip_reason))
            continue
        if matched:
            todo.append(cand)
        else:
            compliant.append(cand)
            log.info("OK  %s — compliant (%s, min effective PPI %s)",
                     cand.path.name, cand.probe["cs"],
                     "%.1f" % cand.min_effective_ppi if cand.min_effective_ppi else "?")

    log.info("")
    log.info("Already compliant         : %d", len(compliant))
    log.info("To process                : %d", len(todo))

    lowres = [c for c in candidates if c.needs_ai_upscale is not None]
    if lowres:
        limit = float(cfg.get("upscale_min_ppi") or 0)
        planned = [c for c in lowres if c.resample_kind == "ai_upscale"]
        blocked = [c for c in lowres if c.resample_kind != "ai_upscale"]
        log.info("")
        log.warning("--- Below %.0f ppi: %d (AI upscale: %d, without: %d) ---",
                    limit, len(lowres), len(planned), len(blocked))
        for c in sorted(planned, key=lambda x: x.needs_ai_upscale):
            pages = ", ".join(sorted({str(p.get("page") or "?") for p in c.placements}))
            log.warning("    %.0f ppi  %s  (page %s) -> AI upscale x%.2f",
                        c.needs_ai_upscale, c.path.name, pages, c.resize_factor)
        for c in sorted(blocked, key=lambda x: x.needs_ai_upscale):
            pages = ", ".join(sorted({str(p.get("page") or "?") for p in c.placements}))
            log.warning("    %.0f ppi  %s  (page %s) -> no AI: %s",
                        c.needs_ai_upscale, c.path.name, pages,
                        c.ai_skip_reason or "причина не указана")

    # Геометрическая проверка — считается по всем связям, включая векторные
    # и битые, и одинаково работает в боевом режиме и в --dry-run.
    bleed_issues = (check_bleed(data.get("links", []), cfg)
                    if stage_on(cfg, "bleed") else [])
    log_bleed_issues(bleed_issues, data, cfg)

    # Живой текст и плашечные краски в векторных связях (.ai/.eps/.pdf/.svg).
    # Чтение файлов на диске, документ не трогается — работает и в --dry-run.
    vectors = []
    if check_text(cfg) or check_spots(cfg):
        vectors = group_vector_links(data.get("links", []), cfg)
        for vc in vectors:
            vc.scan = scan_vector_file(vc.path)
            # Выключенная проверка не должна попадать ни в лог, ни в отчёт —
            # вычищаем её находки сразу после разбора файла.
            if not check_text(cfg):
                vc.scan["hasText"] = False
                vc.scan["fonts"] = []
            if not check_spots(cfg):
                vc.scan["spots"] = []
            plan_vector(vc, cfg)
    log_vector_findings(vectors, cfg, args.dry_run)

    # 3. Обработка ----------------------------------------------------------
    originals_dir = doc_dir / cfg["originals_dir_name"]
    mapping = []
    # Имена, занятые этим прогоном: нужны режиму «в отдельную папку»,
    # где сходятся файлы из разных папок связей.
    taken_out_paths = set()
    master_items = []
    master_used = set()

    # Плашечные каналы сводит Photoshop — до magick, чтобы дальше по цепочке
    # шёл уже обычный CMYK-файл.
    if not args.dry_run:
        run_spot_merge_stage(cfg, [c for c in todo if c.needs_spot_merge],
                             workdir, target_desc)

    # Памятные цвета у картинок, которые в остальном уже в норме. Их не тронул
    # бы никто: по разрешению и цвету претензий нет, а кожа при этом вполне
    # может быть серой. Если правка понадобилась, файл переходит в обработку —
    # переписать его всё равно придётся, чтобы правка попала в вёрстку.
    if check_memory_colors(cfg) and compliant:
        log.info("")
        log.info("--- Memory colours in otherwise compliant files (%d) ---",
                 len(compliant))
        for k, cand in enumerate(list(compliant), 1):
            log.info("")
            log.info("[%d/%d] %s", k, len(compliant), cand.path.name)
            pre_icc = None
            if not cand.probe["has_icc"]:
                pre_icc = assumed_src_icc(cfg, cand.probe["cs"])
            out = color_correct_candidate(
                cfg, cand, cand.path, pre_icc, workdir, "c%03d" % k,
                apply_changes=want_color_correct(cfg) and not args.dry_run)
            if out is None:
                continue
            cand.reasons.append(
                "памятные цвета выходили за норму — файл переписан ради правки")
            compliant.remove(cand)
            todo.append(cand)

    if todo:
        log.info("")
        log.info("--- Processing ---")
    else:
        log.info("")
        log.info("No raster files to process.")
    for n, cand in enumerate(todo, 1):
        if cand.result and cand.result.get("status") == "error":
            continue                       # не сведены плашки — файл не трогаем
        info = cand.probe
        pages = sorted({str(p.get("page") or "?") for p in cand.placements})
        scales = ["%.1f%%" % p["scaleH"] for p in cand.placements
                  if isinstance(p.get("scaleH"), (int, float))]

        log.info("")
        log.info("[%d/%d] %s", n, len(todo), cand.path.name)
        emit("image", index=n, total=len(todo), name=cand.path.name,
             path=str(cand.path))
        log.info("        path        : %s", cand.path)
        log.info("        placements  : %d (page %s; scale %s)",
                 len(cand.placements), ", ".join(pages), ", ".join(scales) or "?")
        log.info("        before      : %s", describe_probe(info))
        log.info("        effective   : min %.1f / max %.1f ppi (actual in InDesign %.1f)",
                 cand.min_effective_ppi or 0, cand.max_effective_ppi or 0,
                 cand.actual_ppi or 0)
        for r in say_all(cand.reasons, LOG_LANG):
            log.info("        reason      : %s", r)
        for w in say_all(cand.warnings, LOG_LANG):
            log.warning("        ! %s", w)

        # Исходный профиль присваиваем только если своего нет.
        src_icc = None
        if cand.needs_color and not info["has_icc"]:
            src_icc = assumed_src_icc(cfg, info["cs"])

        # Что можно отрезать по фрейму и встанет ли файл в макете ровно 100%.
        # Решается до всего остального: от этого зависит и плотность файла,
        # и сколько пикселей поедет через ИИ-апскейл и Photoshop.
        cand.crop = plan_crop(cand, cfg)

        # Плотность нового файла.
        #
        # Обычный случай: физический размер в макете не должен измениться при
        # сохранении масштаба фрейма, отсюда new_density = actual * (new/old).
        #
        # С нормировкой масштаба (scale_to_100) считается наоборот: плотность
        # подбирается так, чтобы картинка встала во фрейм РОВНО 100%, — то есть
        # столько пикселей на дюйм, сколько их приходится на физический размер
        # обрезанного куска. Физический размер при этом тот же самый, меняется
        # только число в палитре.
        actual = cand.actual_ppi or (info["ppi_x"] if info["ppi_x"] > 0 else 72.0)
        base_w = (cand.crop["box"][2] if (cand.crop and cand.crop["cropped"])
                  else info["width"])
        real_factor = (target_width(cand) / float(base_w)) if cand.needs_resample else 1.0
        if cand.crop and want_scale_100(cfg):
            new_density = target_width(cand) * 72.0 / cand.crop["widthPt"]
        else:
            new_density = actual * real_factor

        out_fmt, forced_why = output_format_for(cfg, cand)
        ext = _FORMAT_EXT[out_fmt]
        if forced_why:
            log.warning("        format      : %s instead of %s — %s",
                        out_fmt.upper(), output_format(cfg).upper(), forced_why)

        # Куда писать результат — ОДНА настройка на оба случая, и на вёрстку
        # тоже. Раньше вёрстка её не спрашивала: место результата выходило
        # побочным следствием того, что делали с оригиналом (уехал в
        # originals/ — результат занял его имя, остался — получил суффикс).
        # Объяснить такое человеку нечем, а связь переподключается на любой
        # путь, так что запрета на выбор здесь никогда и не было.
        mode = files_output_mode(cfg)
        if mode == "folder":
            out_dir = files_output_dir(cfg, doc_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (cand.path.stem + ext)
            # Имена в общей папке могут совпасть: в вёрстке связи приходят
            # из разных папок, и два «photo.jpg» затёрли бы друг друга.
            # Занятые ЭТИМ прогоном имена помним; чужие файлы перезаписываем
            # намеренно — повторный прогон должен обновлять папку, а не
            # плодить «photo_2».
            if out_path in taken_out_paths:
                out_path = unique_path(out_path)
            taken_out_paths.add(out_path)
        elif mode == "suffix":
            out_path = unique_path(cand.path.with_name(
                cand.path.stem + image_suffix(cfg) + ext))
        else:
            out_path = cand.path.with_suffix(ext)
            if out_path.exists() and out_path.resolve() != cand.path.resolve():
                out_path = unique_path(cand.path.with_name(
                    cand.path.stem + image_suffix(cfg) + ext))
                log.warning("        ! %s%s already exists — writing to %s",
                            cand.path.stem, ext, out_path.name)
            elif (keep_originals_in_place(cfg)
                  and out_path.resolve() == cand.path.resolve()):
                # У исходника то же расширение, и по имени результат встал бы
                # на его место. В режиме keep исходник обязан уцелеть: пишем
                # рядом. С JPEG на выходе это обычный случай, а не редкость:
                # большинство исходников в вёрстке — те же .jpg.
                out_path = unique_path(cand.path.with_name(
                    cand.path.stem + image_suffix(cfg) + ext))
                log.info("        source      : kept in place, result -> %s",
                         out_path.name)
        in_place = out_path.resolve() == cand.path.resolve()

        # --- обрезка по фрейму ----------------------------------------------
        # Идёт первой: всё, что дальше — ИИ-апскейл, памятные цвета,
        # цветоделение — не должно работать по пикселям, которых в макете
        # не видно.
        if cand.crop is None and (check_crop(cfg) or want_scale_100(cfg)):
            log.info("        crop        : skipped — %s", cand.crop_note)
        elif cand.crop and want_scale_100(cfg) and not cand.crop["cropped"]:
            log.info("        scale       : set to 100%% (was %.1f%%) — "
                     "file density matched to the physical size in the layout",
                     float(cand.placements[0].get("scaleH") or 0))
        if cand.crop and cand.crop["cropped"]:
            x, y, w, h = cand.crop["box"]
            log.info("        crop        : %dx%d+%d+%d — %.0f%% of pixels remain "
                     "(margin %g mm where available)",
                     w, h, x, y, (1 - cand.crop["saving"]) * 100,
                     crop_cfg(cfg).get("margin_mm", 10))
            if not args.dry_run:
                # Промежуточный файл всегда TIFF: обрезка JPEG в JPEG — это
                # лишнее перекодирование там, где его можно не делать вовсе.
                # Формат исходника здесь ничего не решает: файл временный,
                # и дальше его всё равно перепишет magick.
                crop_out = workdir / ("crop_%03d.tif" % n)
                crop_cmd = [cfg["magick"], "%s[0]" % str(cand.spot_source or cand.path)]
                crop_cmd += crop_args(cand)
                crop_cmd += ["-compress", cfg["tiff_compression"], str(crop_out)]
                log.info("        command     : %s", " ".join(_shq(c) for c in crop_cmd))
                log_stage("magick: crop to frame", ui="crop to frame")
                crop_proc = subprocess.run(crop_cmd, capture_output=True, text=True)
                if crop_proc.returncode == 0 and crop_out.exists():
                    cand.crop_source = crop_out
                else:
                    # Не обрезали — не беда: дальше пойдёт целая картинка,
                    # а масштаб 100% станет невозможен, о чём и скажем.
                    log.error("        ERROR       : crop failed (%s) — "
                              "the whole file is processed",
                              (crop_proc.stderr or "").strip()
                              or "code %d" % crop_proc.returncode)
                    cand.crop = None
                    cand.crop_note = "crop failed"

        # --- ИИ-апскейл ---------------------------------------------------
        # Модель не понимает ICC и всегда отдаёт sRGB PNG, поэтому дальше по
        # цепочке исходным профилем считается sRGB, а не то, что было в файле.
        ai_source = None
        ai_cmd = None
        if cand.resample_kind == "ai_upscale":
            src_icc = cfg["assume_rgb_icc"]
            if args.dry_run:
                binary, _models = ai_paths(cfg)
                log.info("        plan        : AI upscale %s x%s (%s), then shrink",
                         ai_cfg(cfg).get("model"), ai_cfg(cfg).get("scale"), binary.name)
            else:
                try:
                    log_stage("AI upscale (Real-ESRGAN)", ui="AI upscale")
                    ai_source, ai_steps, ai_cmd = run_ai_upscale(
                        cfg, cand.crop_source or cand.spot_source or cand.path,
                        workdir, "%03d" % n)
                    cand.ai_done = True
                    for st in ai_steps:
                        log.info("        plan        : %s", st)
                    log.info("        AI command  : %s", " ".join(_shq(c) for c in ai_cmd))
                except Exception as exc:
                    # Без апскейла тянуть картинку интерполяцией не будем —
                    # пользователь отдельно решил, что ниже 200 ppi так нельзя.
                    log.error("        ERROR       : AI upscale failed (%s) — "
                              "resolution left as is", exc)
                    cand.ai_skip_reason = str(exc)
                    cand.needs_resample = False
                    cand.resample_kind = None
                    cand.resize_factor = 1.0
                    new_density = actual
                    src_icc = None
                    if cand.needs_color and not info["has_icc"]:
                        src_icc = cfg["assume_rgb_icc"]

        # --- даунсэмпл перед памятными цветами -------------------------------
        # Ресемплинг обычно живёт в общем вызове magick вместе с цветом и
        # резкостью. Но если дальше файл идёт в Photoshop на памятные цвета,
        # дешевле уменьшить его заранее: модели распознавания стоят одинаково
        # на любом размере, а вот открытие, маски, кривые и запись — прямо
        # пропорционально пикселям. На кадрах с полнокадровой камеры это
        # разница между 50 и 3 мегапикселями.
        if (check_memory_colors(cfg) and cand.needs_resample
                and not cand.color_corrected and not args.dry_run):
            pre_out = workdir / ("pre_%03d.tif" % n)
            pre_cmd, pre_note = build_resize_command(
                cfg, cand, pre_out,
                ai_source or cand.crop_source or cand.spot_source or cand.path)
            log_stage("magick: downsample before correction", ui="downsampling")
            log.info("        plan        : %s (before memory colours)", pre_note)
            pre_proc = subprocess.run(pre_cmd, capture_output=True, text=True)
            if pre_proc.returncode == 0 and pre_out.exists():
                cand.pre_source = pre_out
                cand.resample_done = True
            else:
                # Не беда: ресемплинг просто останется в общем вызове magick,
                # а Photoshop поработает с исходным размером — медленнее,
                # но результат тот же.
                log.warning("        ! separate resampling failed (%s) — "
                            "leaving it to the main magick call",
                            (pre_proc.stderr or "").strip() or "code %d" % pre_proc.returncode)

        # --- вытягивание недодержанного человека -----------------------------
        # Идёт ДО общего тона и памятных цветов: их замеры должны видеть
        # кожу, а не силуэт. Этап свой, от выбора движка не зависит —
        # считают его Vision и magick, Adobe тут не нужен вовсе.
        spec_lift = lift_spec(cfg)
        if spec_lift["enabled"] and not args.dry_run and not cand.color_corrected:
            lift_src = (cand.pre_source or ai_source or cand.crop_source
                        or cand.spot_source or cand.path)
            log_stage("lifting underexposed frames", ui="lifting the frame")
            try:
                lf_out, lf_note = native_lift(cfg, cand, lift_src, workdir,
                                              "%03d" % n, apply=spec_lift["apply"])
            except Exception as exc:
                lf_out, lf_note = None, str(exc)
            if lf_out is not None:
                ok_cs, was_cs, now_cs = same_colorspace(cfg, lift_src, lf_out)
                if not ok_cs:
                    # То же правило, что и для чужого тона: вернул другое
                    # пространство — результат отбрасываем, а не чиним.
                    log.warning("        ! lift dropped: %s instead of %s",
                                now_cs or "?", was_cs or "?")
                    lf_out = None
            log.info("        light       : %s%s", lf_note,
                     "" if lf_out is None else " -> " + Path(lf_out).name)
            note_skin_distrust(cand)
            if lf_out is not None:
                cand.pre_source = lf_out

        # --- общий тон движком Core Image ------------------------------------
        # Единственный движок, которому Adobe не нужен вовсе. Photoshop дальше
        # получает уже поправленный файл и занимается только памятными цветами.
        # Общий тон правится ТОЛЬКО в RGB — так же, как в ветке Photoshop.
        # Core Image, получив CMYK, отдаёт sRGB и не говорит об этом ни слова;
        # дальше файл шёл в цветоделение как «CMYK» и превращался в кашу.
        tone_cs = str((cand.probe or {}).get("cs") or "").upper()
        if (check_memory_colors(cfg) and tone_engine(cfg) == "coreimage"
                and tone_spec(cfg)["enabled"] and tone_spec(cfg)["apply"]
                and not args.dry_run and not cand.color_corrected
                and tone_cs not in ("CMYK", "GRAY")):
            ci_src = (cand.pre_source or ai_source or cand.crop_source
                      or cand.spot_source or cand.path)
            log_stage("overall tone (Core Image)", ui="overall tone")
            before = measure_mean_rgb(cfg, ci_src)
            ci_out, ci_note = run_core_image_tone(cfg, ci_src, workdir, "%03d" % n)
            if ci_out is not None:
                ok_cs, was_cs, now_cs = same_colorspace(cfg, ci_src, ci_out)
                if not ok_cs:
                    # Не чиним, а отказываемся: чужой движок вернул файл
                    # в другом пространстве, и дальше по цепочке его примут
                    # не за то, что он есть.
                    cand.warnings.append(msg("tone_dropped_cs", got=now_cs or "?",
                                             was=was_cs or "?"))
                    log.warning("        ! %s", say(cand.warnings[-1], LOG_LANG))
                    ci_out = None
            if ci_out is None:
                cand.warnings.append(msg("tone_failed", why=ci_note))
                log.warning("        ! %s", say(cand.warnings[-1], LOG_LANG))
            else:
                try:
                    # Свои точки просят у чужого движка только светлоту: цвет
                    # они считают сами, и подмешивать сверх того значит править
                    # его дважды.
                    points = bool((cfg.get("global_tone") or {}).get("neutral_points"))
                    tone_cfg = (dict(cfg, global_tone=dict(cfg.get("global_tone") or {},
                                                           color_strength=0.0))
                                if points else cfg)
                    ci_out, limit_note = limit_foreign_tone(tone_cfg, ci_src, ci_out,
                                                            workdir, "%03d" % n)
                    if limit_note:
                        ci_note = "%s; limited: %s" % (ci_note, limit_note)
                    if points:
                        pt_out, pt_note = native_tone_points(cfg, cand, ci_out,
                                                             src_icc, workdir, "%03d" % n)
                        if pt_out is not None:
                            ci_out = pt_out
                        ci_note = "%s; points: %s" % (ci_note, pt_note)
                except Exception as exc:
                    # Ограничитель — улучшение, а не необходимость: не вышло —
                    # берём результат движка как есть и говорим об этом.
                    cand.warnings.append(msg("tone_unlimited", why=str(exc)))
                    log.warning("        ! %s", say(cand.warnings[-1], LOG_LANG))
                cand.pre_source = ci_out
                after = measure_mean_rgb(cfg, ci_out)
                cand.color_findings = list(cand.color_findings) + [{
                    "color": "tone", "part": "", "status": "corrected",
                    "why": "Core Image (%s): mean %s -> %s" % (ci_note, before, after),
                    "area": 100, "before": "", "after": "", "shift": "",
                }]
                log.info("        colour      : overall tone — Core Image (%s): "
                         "mean %s -> %s", ci_note, before, after)

        # --- памятные цвета -------------------------------------------------
        # Идёт ДО цветоделения и после ИИ-апскейла: правка кладётся в исходное
        # пространство файла, иначе картинка прошла бы конверсию дважды.
        if check_memory_colors(cfg) and not cand.color_corrected:
            if args.dry_run:
                log.info("        plan        : measuring memory colours (%s)",
                         ", ".join(MEMORY_COLOR_TITLES[s["name"]]
                                   for s in memory_color_specs(cfg)))
                color_correct_candidate(cfg, cand, ai_source or cand.crop_source
                                        or cand.spot_source or cand.path,
                                        src_icc, workdir,
                                        "%03d" % n, apply_changes=False)
            else:
                corrected = color_correct_candidate(
                    cfg, cand, cand.pre_source or ai_source or cand.crop_source
                    or cand.spot_source or cand.path,
                    src_icc, workdir, "%03d" % n,
                    apply_changes=want_color_correct(cfg))
                if corrected is not None:
                    log.info("        plan        : memory colours corrected -> %s",
                             corrected.name)

        # --- мастер для второй версии номера --------------------------------
        # Момент выбран точно: всё общее (обрезка, ИИ-апскейл, памятные цвета,
        # ресемплинг) уже сделано, а расходящееся (цветоделение и шарпинг) —
        # ещё нет. Именно этот файл и нужен планшетной версии.
        if check_master(cfg) and not args.dry_run:
            steps_done = []
            if cand.crop and cand.crop["cropped"]:
                steps_done.append("crop to frame")
            if cand.ai_done:
                steps_done.append("AI upscale")
            if cand.color_corrected:
                steps_done.append("memory colours")
            if cand.resample_done or cand.needs_resample:
                steps_done.append("resample to %g ppi" % cfg["target_effective_ppi"])
            try:
                item = write_master(cfg, cand,
                                    cand.correct_source or cand.pre_source or ai_source
                                    or cand.crop_source or cand.path,
                                    doc_dir, steps_done, master_used)
            except Exception as exc:
                item = None
                log.warning("        ! master not written (%s) — this does not "
                            "affect print; the tablet edition will have to "
                            "compute it itself", exc)
            if item is not None:
                master_items.append(item)
                log.info("        master      : %s (%s)", item["master"],
                         ", ".join(steps_done) or "без изменений")

        # --- цветоделение в Photoshop --------------------------------------
        # Встаёт между ИИ-апскейлом и magick: апскейл обязан отработать по RGB,
        # а magick дальше только ресемплит, шарпит и пишет TIFF.
        if cand.needs_color:
            cand.color_by = color_engine(cfg)
        if cand.needs_color and color_engine(cfg) == "photoshop":
            if args.dry_run:
                log.info("        plan        : separation in Photoshop "
                         '-> "%s" (%s%s)',
                         icc_description(cfg, target_icc_for(cfg, cand.probe["cs"]))
                         or Path(target_icc_for(cfg, cand.probe["cs"])).name,
                         cfg["rendering_intent"],
                         " + BPC" if cfg.get("black_point_compensation") else "")
            else:
                try:
                    log_stage("separation in Photoshop", ui="colour separation")
                    cand.color_source = run_photoshop_color(
                        cfg, cand, cand.correct_source or cand.pre_source
                        or ai_source or cand.crop_source or cand.spot_source
                        or cand.path,
                        src_icc, workdir, "%03d" % n)
                    cand.color_by = "photoshop"
                    cand.needs_color = False
                    src_icc = None
                    log.info("        plan        : colour separated in Photoshop -> %s",
                             cand.color_source.name)
                except Exception as exc:
                    # Не падаем: magick умеет то же самое теми же профилями.
                    # Но молчать нельзя — движок выбран человеком осознанно,
                    # и подмена должна быть видна и в логе, и в отчёте.
                    note = ("цветоделение в Photoshop не удалось (%s) — "
                            "файл поделён magick" % exc)
                    cand.color_by = "magick"
                    cand.warnings.append(note)
                    log.error("        ERROR       : %s", note)

        cmd, steps = build_command(cfg, cand, out_path, src_icc, new_density,
                                   source=cand.color_source or cand.correct_source
                                   or cand.pre_source or ai_source or cand.crop_source
                                   or cand.spot_source)
        if cand.color_source:
            steps.insert(0, "source taken after separation in Photoshop")
        elif cand.correct_source:
            steps.insert(0, "source taken after the memory-colour correction")
        if cand.spot_source and not ai_source:
            steps.insert(0, "source taken after merging spot channels in Photoshop")
        elif cand.needs_spot_merge and args.dry_run:
            steps.insert(0, "сначала Photoshop сведёт плашечные каналы и переведёт "
                            "файл в целевой профиль, дальше magick работает с ним")
        for s in steps:
            log.info("        plan        : %s", s)
        log.info("        command     : %s", " ".join(_shq(c) for c in cmd))

        if args.dry_run:
            cand.result = {"status": "dry-run", "out": str(out_path)}
            continue

        # Если исходник сам .tif и имя совпадает — пишем во временный файл,
        # чтобы не затереть оригинал до того, как он уедет в originals.
        tmp_out = None
        if in_place:
            # Расширение обязано совпасть с целевым: формат записи magick
            # выбирает по нему, а не по флагам.
            tmp_out = Path(tempfile.mkstemp(suffix=out_path.suffix,
                                            dir=str(cand.path.parent))[1])
            cmd[-1] = str(tmp_out)

        stage_parts = []
        if cand.needs_resample:
            stage_parts.append("resample")
        if cand.needs_color:
            stage_parts.append("color")
        stage_parts.append("write")
        log_stage("magick: " + ", ".join(stage_parts), ui=", ".join(stage_parts))

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stderr.strip():
            log.warning("        magick      : %s", proc.stderr.strip())
        if proc.returncode != 0:
            log.error("        ERROR       : magick returned %d — file skipped",
                      proc.returncode)
            if tmp_out and tmp_out.exists():
                tmp_out.unlink()
            cand.result = {"status": "error", "error": proc.stderr.strip()}
            continue

        # Оригинал в originals (до того, как встанет TIFF на его место).
        # В режиме keep он никуда не девается — резервом служит он сам.
        dest = None
        if not keep_originals_in_place(cfg):
            dest = originals_target(cand, doc_dir, originals_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if cfg["originals_mode"] == "move":
                shutil.move(str(cand.path), str(dest))
            else:
                shutil.copy2(str(cand.path), str(dest))

        if tmp_out:
            shutil.move(str(tmp_out), str(out_path))

        after = probe_image(cfg, out_path)
        # Сторож последней черты: пиксели и профиль должны быть про одно и то же.
        # Файл с RGB-пикселями и наклеенным CMYK-профилем выглядит в вёрстке
        # нормально и ломается уже на выводе. Появляется он не от сбоя, а от
        # потерянного по дороге ICC: magick без исходного профиля НЕ конвертирует
        # по `-profile`, а присваивает. Проверка стоит один identify на файл.
        if cand.needs_color:
            # Что должно получиться — зависит от направления перевода: при
            # обратном CMYK на выходе как раз и был бы ошибкой.
            if color_direction(cfg) == "to_rgb":
                want, human = "RGB", "RGB"
            elif str(cand.probe.get("cs", "")).upper().startswith("GRAY") \
                    and not cfg.get("convert_grayscale_to_cmyk"):
                want, human = "GRAY", "Grayscale"
            else:
                want, human = "CMYK", "CMYK"
            got = str(after.get("cs", "")).upper()
            ok = (want in got) or (want == "RGB" and "RGB" in got)
            if want in ("CMYK", "RGB") and not ok:
                note = msg("wrong_colorspace", got=got or "?", want=human,
                           icc=after.get("icc_desc") or "—")
                cand.warnings.append(note)
                log.error("        ERROR       : %s", note)
        new_eff = {}
        for p in cand.placements:
            sh = p.get("scaleH") or 100.0
            new_eff[str(p.get("page") or "?")] = after["ppi_x"] / (sh / 100.0)

        log.info("        after       : %s", describe_probe(after))
        log.info("        effective   : %s ppi",
                 ", ".join("p.%s %.1f" % (k, v) for k, v in sorted(new_eff.items())))
        if dest is None:
            log.info("        original    : kept in place (%s)", cand.path.name)
        else:
            log.info("        original    : %s -> %s",
                     "moved" if cfg["originals_mode"] == "move" else "copied",
                     dest.relative_to(doc_dir) if dest.is_relative_to(doc_dir) else dest)
        log.info("        new file    : %s", out_path.name)

        # Промежуточные файлы этой картинки больше не нужны. Раньше они
        # копились до конца прогона, а теперь их на каждую картинку по четыре
        # (обрезка, ресемплинг, памятные цвета, цветоделение): на вёрстке
        # из сотни связей это гигабайты, и прогон однажды убила нехватка места.
        for tmp_file in (cand.crop_source, cand.pre_source, cand.correct_source,
                         cand.color_source, cand.spot_source, ai_source):
            if tmp_file and Path(tmp_file).parent == workdir:
                try:
                    Path(tmp_file).unlink()
                except OSError:
                    pass

        cand.out_path = out_path
        cand.result = {
            "status": "ok",
            "out": str(out_path),
            "original_moved_to": str(dest) if dest else None,
            "originalKeptInPlace": dest is None,
            "before": info,
            "after": after,
            "command": cmd,
            "colorBy": cand.color_by,
            "new_effective_ppi": new_eff,
        }
        # Адресуем связи теми же строками путей, которые отдал сам InDesign.
        for raw in cand.link_paths:
            entry = {"oldPath": raw, "newPath": str(out_path)}
            if cand.crop and (want_scale_100(cfg) or cand.crop["cropped"]):
                # relink.jsx вернёт картинку на место: после обрезки её левый
                # верхний угол обязан оказаться именно там, где кончился
                # отрезанный запас. Без этого содержимое во фрейме уехало бы.
                #
                # Масштаб: 100% при нормировке, иначе прежний — обрезанному
                # файлу указание нужно в любом случае, иначе InDesign оставит
                # картинку прижатой к прежнему углу.
                entry["place"] = {
                    "scale": 100.0 if want_scale_100(cfg)
                             else float(cand.placements[0].get("scaleH") or 100.0),
                    "padLeftPt": cand.crop["padLeftPt"],
                    "padTopPt": cand.crop["padTopPt"],
                }
            mapping.append(entry)

    if check_master(cfg) and master_items and not args.dry_run:
        mpath = write_master_manifest(cfg, doc_dir, doc_real, master_items)
        log.info("")
        log.info("--- Masters for the digital edition ---")
        log.info("Masters kept: %d", len(master_items))
        log.info("Folder  : %s", master_dir(cfg, doc_dir))
        log.info("Manifest: %s", mpath)
        log.info("These are files BEFORE separation and WITHOUT sharpening: crop, AI upscale "
                 "and memory colours are already applied.")

    # Текст в кривые и плашки в CMYK в векторных связях.
    outline_report = None
    if not args.dry_run:
        outline_report = run_outline_stage(cfg, vectors, doc_dir, originals_dir,
                                           workdir, mapping, target_desc)

    # 4. Переподключение ----------------------------------------------------
    relink_report = None
    if not stage_on(cfg, "relink"):
        log.info("")
        log.info("--- Relink ---")
        log.info("Skipped: the stage is not in the chain. Processed files are "
                 "on disk; the layout keeps its previous links.")
    elif args.dry_run:
        log.info("")
        log.info("--- Relink ---")
        log.info("Skipped: --dry-run mode.")
    elif args.no_relink:
        log.info("")
        log.info("--- Relink ---")
        log.info("Skipped by --no-relink. Update the links in InDesign manually.")
    elif not mapping and args.no_save:
        log.info("")
        log.info("No file was processed, saving is off (--no-save) — "
                 "the document is left alone.")
    else:
        log.info("")
        if mapping:
            log.info("--- Relinking in InDesign (%d files) ---",
                     len({m["newPath"] for m in mapping}))
            log_stage("relinking, saving")
        else:
            # Ни одна картинка не потребовала обработки — все уже соответствовали
            # требованиям. Копию с суффиксом всё равно делаем: дальше по цепочке
            # ждут именно её, и «нечего было делать» не повод не отдать файл.
            # Когда суффикс уже в имени (save_as пуст), сохраняются только
            # несохранённые правки, если они есть, — решение принимает
            # relink.jsx по doc.modified, чтобы не переписывать файл впустую.
            log.info("--- Saving the document (nothing to relink) ---")
            log_stage("saving the document")
        relink_report = relink_document(cfg, str(doc_real), mapping, workdir,
                                        save=not args.no_save, save_as=save_as,
                                        was_modified=bool(data.get("documentModified")))
        # Один файл может фигурировать под несколькими вариантами пути —
        # ошибкой считаем только полное отсутствие переподключений для файла.
        by_new = {}
        for r in relink_report.get("results", []):
            by_new.setdefault(r["newPath"], []).append(r)
        for new_path, rs in by_new.items():
            total = sum(r.get("relinked", 0) for r in rs)
            errors = [r["error"] for r in rs if r.get("error")]
            scale_notes = [r["scaleRestored"] for r in rs if r.get("scaleRestored")]
            if total > 0 and not errors:
                log.info("    OK  %s (links: %d)%s", Path(new_path).name, total,
                         "  [масштаб восстановлен: %s]" % "; ".join(scale_notes)
                         if scale_notes else "")
            else:
                log.error("    !!  %s — %s", Path(new_path).name,
                          "; ".join(errors) or "связь не найдена в документе")

        if mapping:
            log.info("Links relinked in total: %s", relink_report.get("relinkedTotal"))
        if relink_report.get("saved"):
            saved_to = relink_report.get("savedTo")
            if save_as and saved_to:
                log.info("Document saved TO A NEW FILE: %s", saved_to)
                if mapping:
                    log.info("The source %s is unchanged — it still links "
                             "to the original images.", doc_real.name)
                else:
                    log.info("There was nothing to process, so the copy repeats "
                             "%s link for link.", doc_real.name)
            else:
                log.info("Document saved.")
        elif relink_report.get("saveError"):
            log.error("Document NOT saved: %s", relink_report["saveError"])
        elif args.no_save:
            log.info("Document not saved (--no-save).")
        elif not mapping:
            log.info("Document not saved: there was nothing to process, "
                     "and it has no unsaved changes.")
        else:
            log.warning("Document not saved: no link was relinked.")

    result = finish(cfg, args, doc_real, logs_dir, base, started,
                    data, candidates, todo, compliant, skipped, relink_report,
                    bleed_issues, vectors, save_as)
    # Рабочая папка удаляется только на удачном финише: после сбоя в ней
    # лежит всё, что успело посчитаться, и это единственный способ понять,
    # на чём именно споткнулись.
    shutil.rmtree(workdir, ignore_errors=True)
    return result


def _shq(s):
    s = str(s)
    return s if re.match(r"^[\w./=%+:-]+$", s) else "'%s'" % s.replace("'", "'\\''")


def finish(cfg, args, doc_real, logs_dir, base, started,
           data, candidates, todo, compliant, skipped, relink_report,
           bleed_issues=None, vectors=None, save_as=None):
    """Пишет JSON-манифест и markdown-отчёт, печатает итог."""
    elapsed = (_dt.datetime.now() - started).total_seconds()
    ok = [c for c in todo if c.result and c.result.get("status") == "ok"]
    errs = [c for c in todo if c.result and c.result.get("status") == "error"]

    manifest = {
        "schema": 1,
        "startedAt": started.isoformat(timespec="seconds"),
        "elapsedSec": round(elapsed, 1),
        "document": str(doc_real),
        "documentSavedAs": str(save_as) if save_as else None,
        "indesignVersion": data.get("appVersion"),
        "config": {k: v for k, v in cfg.items()},
        "dryRun": bool(args.dry_run),
        "counts": {
            "linksTotal": data.get("linkCount"),
            "rasterFiles": len(candidates),
            "compliant": len(compliant),
            "processed": len(ok),
            "failed": len(errs),
            "skipped": len(skipped),
            "vectorLinks": len(vectors or []),
            "vectorWithText": len([c for c in (vectors or []) if c.has_text]),
            "vectorWithSpots": len([c for c in (vectors or []) if c.spots]),
            "vectorFixed": len([c for c in (vectors or [])
                                if c.result and c.result.get("status") == "ok"]),
            "spotRasters": len([c for c in candidates if c.spot_info]),
        },
        "skipped": [{"name": n, "reason": r} for n, r in skipped],
        "bleed": {
            "requiredMm": cfg.get("bleed_required_mm"),
            "scope": cfg.get("bleed_scope"),
            "documentSetupMm": {
                "top": data.get("bleedTopMm"), "left": data.get("bleedLeftMm"),
                "bottom": data.get("bleedBottomMm"), "right": data.get("bleedRightMm"),
            },
            "issues": bleed_issues or [],
        },
        "features": {
            "textInLinks": {
                "checked": check_text(cfg),
                "convertToOutlines": want_outlines(cfg),
            },
            "spotColors": {
                "checked": check_spots(cfg),
                "convertToCmyk": want_spot_convert(cfg),
            },
            "cropToFrame": {
                "checked": check_crop(cfg),
                "marginMm": crop_cfg(cfg).get("margin_mm"),
                "scaleTo100": want_scale_100(cfg),
            },
            "memoryColors": {
                "checked": check_memory_colors(cfg),
                "apply": want_color_correct(cfg),
                "maxShiftPercent": memory_cfg(cfg).get("max_shift_percent"),
                "colors": [s["name"] for s in memory_color_specs(cfg)]
                          if check_memory_colors(cfg) else [],
            },
        },
        "cropToFrame": [
            {
                "path": str(c.path),
                "cropped": bool(c.crop and c.crop["cropped"]),
                "box": list(c.crop["box"]) if c.crop else None,
                "savingPercent": round(c.crop["saving"] * 100, 1) if c.crop else None,
                "scaleBefore": c.placements[0].get("scaleH") if c.placements else None,
                "scaleAfter": 100.0 if (c.crop and want_scale_100(cfg)) else None,
                "note": c.crop_note,
                "pages": sorted({str(p.get("page") or "?") for p in c.placements}),
            }
            for c in candidates if c.crop or c.crop_note
        ],
        "memoryColors": [
            {
                "path": str(c.path),
                "corrected": c.color_corrected,
                "pages": sorted({str(p.get("page") or "?") for p in c.placements}),
                "findings": c.color_findings,
            }
            for c in candidates if c.color_findings
        ],
        "vectorLinks": [
            {
                "path": str(c.path),
                "kind": c.scan.get("kind"),
                "hasText": c.has_text,
                "fonts": c.fonts,
                "spots": c.spots,
                "pageCount": c.scan.get("pageCount"),
                "scanTruncated": bool(c.scan.get("truncated")),
                "pages": c.pages,
                "plannedActions": c.actions,
                "blocked": c.blocked,
                "outPath": str(c.out_path) if c.out_path else None,
                "notes": c.notes,
                "result": c.result,
            }
            for c in (vectors or [])
        ],
        "spotRasters": [
            {
                "path": str(c.path),
                "spots": c.spot_info["spots"],
                "mode": c.spot_info.get("mode"),
                "channels": c.spot_info.get("channels"),
                "merged": c.spot_merged,
                "mergedInPhotoshop": bool(c.spot_source),
                "pages": sorted({str(p.get("page") or "?") for p in c.placements}),
            }
            for c in candidates if c.spot_info
        ],
        "needsAiUpscale": [
            {"path": str(c.path), "minEffectivePpi": c.needs_ai_upscale,
             "pixels": [c.probe["width"], c.probe["height"]] if c.probe else None,
             "neededScale": round(float(cfg["target_effective_ppi"]) / c.needs_ai_upscale, 3),
             "aiUpscaled": c.ai_done, "skipReason": c.ai_skip_reason,
             "pages": sorted({str(p.get("page") or "?") for p in c.placements})}
            for c in sorted((x for x in candidates if x.needs_ai_upscale is not None),
                            key=lambda x: x.needs_ai_upscale)
        ],
        "compliant": [
            {"path": str(c.path), "colorspace": c.probe["cs"] if c.probe else None,
             "iccDescription": c.probe.get("icc_desc") if c.probe else None,
             "minEffectivePpi": c.min_effective_ppi}
            for c in compliant
        ],
        "processed": [
            {
                "path": str(c.path),
                "reasons": c.reasons,
                "warnings": c.warnings,
                "needsColorConversion": c.needs_color,
                "needsResample": c.needs_resample,
                "resampleKind": c.resample_kind,
                "aiUpscaled": c.ai_done,
                "aiSkipReason": c.ai_skip_reason,
                "resizeFactor": round(c.resize_factor, 6),
                "minEffectivePpi": c.min_effective_ppi,
                "maxEffectivePpi": c.max_effective_ppi,
                "actualPpi": c.actual_ppi,
                "placements": [
                    {"page": p.get("page"), "layer": p.get("layer"),
                     "scaleH": p.get("scaleH"), "scaleV": p.get("scaleV"),
                     "effectivePpiH": p.get("effectivePpiH"),
                     "effectivePpiV": p.get("effectivePpiV"),
                     "clippingType": p.get("clippingType")}
                    for p in c.placements
                ],
                "result": c.result,
            }
            for c in todo
        ],
        "relink": relink_report,
    }

    json_path = logs_dir / (base + ".json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, default=str)

    md_path = logs_dir / (base + ".md")
    write_markdown_report(md_path, manifest, todo, compliant, skipped)

    # HTML рисуется ИЗ МАНИФЕСТА, а не из живых объектов прогона: тот же
    # отчёт можно перерисовать по старому прогону и на другом языке, ничего
    # не пересчитывая. Приложение показывает именно его; markdown остаётся
    # для терминала.
    html_path = logs_dir / (base + ".html")
    try:
        import report_html
        report_html.write(html_path, manifest, report_lang(cfg))
    except Exception as exc:
        html_path = None
        log.warning("HTML report not built (%s) — markdown is in place", exc)

    log.info("")
    log.info("=" * 78)
    log.info("DONE in %.1f s", elapsed)
    log.info("  %s : %d",
             "raster files" if cfg.get("__on_files")
             else "raster files in layout", len(candidates))
    log.info("  already compliant         : %d", len(compliant))
    log.info("  processed                 : %d", len(ok))
    if errs:
        log.info("  failed                    : %d", len(errs))
    if skipped:
        log.info("  not considered            : %d", len(skipped))
    if bleed_issues:
        log.info("  without required bleed    : %d", len(bleed_issues))
    flagged = [c for c in (vectors or []) if c.has_text or c.spots]
    if flagged:
        fixed = [c for c in flagged if c.result and c.result.get("status") == "ok"]
        manual = [c for c in flagged if c.blocked]
        log.info("  vector text/spots         : %d (converted %d, manual %d)",
                 len(flagged), len(fixed), len(manual))
    spot_rasters = [c for c in candidates if c.spot_info]
    if spot_rasters:
        log.info("  raster with spots         : %d (merged %d)", len(spot_rasters),
                 len([c for c in spot_rasters if c.spot_source]))
    if check_crop(cfg):
        cropped = [c for c in candidates if c.crop and c.crop["cropped"]]
        if cropped:
            saved = sum(c.crop["saving"] for c in cropped) / len(cropped) * 100
            log.info("  cropped to frame          : %d (on average -%.0f%% pixels)",
                     len(cropped), saved)
    if check_memory_colors(cfg):
        seen = [c for c in candidates if c.color_findings]
        fixed = [c for c in seen if c.color_corrected]
        log.info("  memory colours            : checked %d, corrected %d",
                 len(seen), len(fixed))
    saved_to = (relink_report or {}).get("savedTo")
    if save_as and saved_to:
        log.info("")
        log.info("  CMYK document   : %s", saved_to)
        log.info("  source          : %s (unchanged)", doc_real.name)
    log.info("")
    log.info("  log      : %s", logs_dir / (base + ".log"))
    log.info("  manifest : %s", json_path)
    log.info("  report   : %s", md_path)
    if html_path:
        log.info("  HTML     : %s", html_path)
    log.info("=" * 78)

    emit("run_done", document=str(doc_real), seconds=round(elapsed, 1),
         processed=len(ok), failed=len(errs),
         warnings=sum(len(c.warnings or []) for c in todo),
         report=str(md_path), reportHtml=str(html_path) if html_path else None,
         manifest=str(json_path),
         log=str(logs_dir / (base + ".log")),
         savedTo=saved_to or None)

    notify(cfg, "Препресс: %s" % doc_real.name,
           "Обработано %d, ошибок %d" % (len(ok), len(errs)))
    open_report(cfg, md_path)
    return manifest


def write_markdown_report(path, m, todo, compliant, skipped):
    # Язык отчёта — из настроек, а не из журнала: отчёт читает человек,
    # журнал читают при разборе сбоя.
    lang = report_lang(m.get("config") or {})
    L = []
    A = L.append
    A("# Препресс-отчёт: %s" % Path(m["document"]).name)
    A("")
    A("- **Документ:** `%s`" % m["document"])
    A("- **Дата:** %s (%.1f с)" % (m["startedAt"].replace("T", " "), m["elapsedSec"]))
    A("- **Режим:** %s" % ("анализ (dry-run)" if m["dryRun"] else "обработка"))
    # Только для боевого прогона. В проверке documentSavedAs тоже заполнен —
    # путь вычисляется заранее, — но сохранять там нечего, и блок сообщал бы
    # «сохранён в … — не сохранён, см. лог» про несуществующий файл.
    if m.get("documentSavedAs") and not m["dryRun"]:
        rel = (m["relink"] or {})
        A("- **Результат сохранён в:** `%s`%s"
          % (m["documentSavedAs"],
             "" if rel.get("saved") else " — **не сохранён**, см. лог"))
        A("- **Исходный документ:** не изменён — в нём остались связи "
          "на исходные картинки")
    # Через полгода по отчёту должно быть видно, каким движком сделан номер:
    # варианты дают разный результат, и «почему эта картинка другая» иначе
    # не разобрать.
    A("- **Движок:** %s" % describe_engine(m["config"]))
    A("- **Целевой профиль:** `%s`" % m["config"]["target_icc"])
    A("- **Профиль для Grayscale:** %s"
      % ("`%s`" % target_icc_for(m["config"], "GRAY")
         if m["config"].get("convert_grayscale_to_cmyk")
         else "не применяется (серые остаются K-only)"))
    A("- **Порог / цель:** effective PPI > %s → %s"
      % (m["config"]["max_effective_ppi"], m["config"]["target_effective_ppi"]))
    A("- **Выход:** %s" % describe_output(m["config"]))
    A("- **Шарпинг:** %s" % ("включён: %s" % m["config"]["sharpen"]["unsharp"]
                             if m["config"]["sharpen"]["enabled"] else "выключен (заглушка)"))

    def feature_line(f, convert_key):
        if not f.get("checked"):
            return "проверка выключена"
        return ("проверка + автоперевод" if f.get(convert_key)
                else "только проверка и предупреждение")

    feats = m.get("features") or {}
    if feats:
        A("- **Живой текст в связях:** %s"
          % feature_line(feats["textInLinks"], "convertToOutlines"))
        A("- **Плашечные краски:** %s"
          % feature_line(feats["spotColors"], "convertToCmyk"))
        mem = feats.get("memoryColors") or {}
        A("- **Памятные цвета:** %s"
          % ("проверка выключена" if not mem.get("checked")
             else "%s — %s" % (", ".join(MEMORY_COLOR_TITLES.get(c, c)
                                         for c in mem.get("colors") or []) or "—",
                               "замер и правка" if mem.get("apply")
                               else "только замер и отчёт")))
    A("")
    c = m["counts"]
    A("| Показатель | Значение |")
    A("|---|---|")
    A("| Связей в документе | %s |" % c["linksTotal"])
    A("| Уникальных растровых файлов | %s |" % c["rasterFiles"])
    A("| Соответствуют требованиям | %s |" % c["compliant"])
    A("| Обработано | %s |" % c["processed"])
    A("| Ошибок | %s |" % c["failed"])
    A("| Не рассматривались | %s |" % c["skipped"])
    A("| Не хватает вылета под обрез | %s |" % len(m.get("bleed", {}).get("issues", [])))
    A("| Векторных связей с живым текстом | %s |" % c.get("vectorWithText", 0))
    A("| Векторных связей с плашками | %s |" % c.get("vectorWithSpots", 0))
    A("| Растровых файлов с плашечными каналами | %s |" % c.get("spotRasters", 0))
    A("")

    # Предупреждения — ПЕРВЫМ разделом, сразу за сводкой. Раньше они стояли
    # пятыми, после обрезки, памятных цветов и таблицы обработанных, и на
    # номере из сотни картинок терялись в тексте: человек читает отчёт ради
    # того, что надо проверить руками, а не ради того, что и так вышло.
    warned = [c2 for c2 in todo if c2.warnings]
    if warned:
        A("## Предупреждения — проверить вручную")
        A("")
        for cand in warned:
            A("- `%s`" % cand.path.name)
            for w in say_all(cand.warnings, lang):
                A("  - %s" % w)
        A("")

    bl = m.get("bleed") or {}
    if bl.get("issues"):
        A("## ⚠ Не хватает вылета под обрез")
        A("")
        A("Норма — %s мм за обрезной формат %s. Считается по видимому содержимому "
          "(пересечение картинки и её фрейма): фрейм может уходить за обрез, "
          "а картинка внутри до края не доставать."
          % (bl.get("requiredMm"), bl["issues"][0].get("scope", "разворота")))
        A("")
        A("| Стр. | Файл | Сторона | Факт. вылет | Из-за чего |")
        A("|---|---|---|---|---|")
        for it in bl["issues"]:
            for e in it["edges"]:
                A("| %s | `%s` | %s | %.1f мм | %s |"
                  % (it.get("page") or "?", it.get("name"), e["edgeName"],
                     e["contentBleedMm"], e["cause"]))
        A("")

    vectors = m.get("vectorLinks") or []
    with_text = [v for v in vectors if v.get("hasText")]
    with_spots = [v for v in vectors if v.get("spots")]

    def vstatus(v):
        r = v.get("result") or {}
        if r.get("status") == "ok":
            done = []
            if r.get("outlined"):
                done.append("текст → кривые (объектов: %s)" % r["outlined"])
            if r.get("spotsConverted"):
                done.append("плашки → CMYK (%s)" % r["spotsConverted"])
            return "✅ %s → `%s`" % ("; ".join(done) or "обработан",
                                     Path(r.get("out") or "").name)
        if r.get("status") == "error":
            return "❌ %s" % r.get("error")
        if v.get("blocked"):
            return "✋ вручную: %s" % v["blocked"]
        if v.get("plannedActions"):
            names = {"outlines": "текст → кривые", "spots": "плашки → CMYK"}
            return "план: %s" % ", ".join(names.get(a, a) for a in v["plannedActions"])
        return "только предупреждение (автоперевод выключен)"

    if with_text:
        A("## ⚠ Живой текст в векторных связях")
        A("")
        A("Типография требует кривые: подставится чужой шрифт — «поедет» вёрстка. "
          "В боевом прогоне Illustrator переводит текст в кривые и сохраняет "
          "рядом новый файл, оригинал уезжает в `originals/`.")
        A("")
        A("| Файл | Стр. | Шрифты | Что сделано |")
        A("|---|---|---|---|")
        for v in with_text:
            A("| `%s` | %s | %s | %s |"
              % (Path(v["path"]).name, ", ".join(v.get("pages") or []) or "?",
                 ", ".join(v.get("fonts") or []) or "не перечислены в файле",
                 vstatus(v)))
        A("")

    if with_spots:
        A("## ⚠ Плашечные краски в векторных связях")
        A("")
        A("Плашки (Pantone и прочие) в CMYK-печать не идут — их надо разложить "
          "в четыре краски. Illustrator делает это своим **рабочим CMYK**: "
          "проверьте, что в Color Settings стоит `%s`."
          % m["config"]["target_icc"])
        A("")
        A("| Файл | Стр. | Плашки | Что сделано |")
        A("|---|---|---|---|")
        for v in with_spots:
            A("| `%s` | %s | %s | %s |"
              % (Path(v["path"]).name, ", ".join(v.get("pages") or []) or "?",
                 ", ".join(v.get("spots") or []), vstatus(v)))
        A("")

    spot_rasters = m.get("spotRasters") or []
    if spot_rasters:
        A("## ⚠ Плашечные каналы в растровых файлах")
        A("")
        A("Дополнительные каналы в TIFF/PSD. В боевом прогоне Photoshop сводит их "
          "в CMYK с профилем `%s`, и только потом файл идёт в обычную обработку."
          % m["config"]["target_icc"])
        A("")
        A("| Файл | Стр. | Режим | Плашки | Сведено |")
        A("|---|---|---|---|---|")
        for r in spot_rasters:
            A("| `%s` | %s | %s | %s | %s |"
              % (Path(r["path"]).name, ", ".join(r.get("pages") or []) or "?",
                 r.get("mode") or "?", ", ".join(r.get("spots") or []),
                 "да (%s)" % ", ".join(r["merged"]) if r.get("merged")
                 else ("да" if r.get("mergedInPhotoshop")
                       else ("план" if m["dryRun"] else "нет"))))
        A("")

    crops = m.get("cropToFrame") or []
    done_crop = [c for c in crops if c.get("cropped")]
    if crops:
        cfeat = (m.get("features") or {}).get("cropToFrame") or {}
        A("## ✂️ Обрезка по фрейму и масштаб 100%")
        A("")
        A("В макет кладут кадр целиком, а видно из него только то, что попало "
          "во фрейм. Обрезается видимая часть плюс запас %s мм с каждой стороны "
          "(там, где он есть) — запас нужен, чтобы картинку потом можно было "
          "подвинуть. Плотность нового файла подобрана так, чтобы в макете он "
          "встал ровно 100%%: физический размер и положение при этом не меняются."
          % (cfeat.get("marginMm") or 10))
        A("")
        A("| Файл | Стр. | Осталось пикселей | Масштаб был | Стал | Примечание |")
        A("|---|---|---|---|---|---|")
        for c in crops:
            A("| `%s` | %s | %s | %s | %s | %s |"
              % (Path(c["path"]).name, ", ".join(c.get("pages") or []) or "?",
                 "%.0f%%" % (100 - (c.get("savingPercent") or 0)) if c.get("cropped") else "—",
                 "%.1f%%" % c["scaleBefore"] if isinstance(c.get("scaleBefore"), (int, float)) else "?",
                 "100%" if c.get("scaleAfter") else "без изменений",
                 c.get("note") or ("обрезан" if c.get("cropped") else "резать нечего")))
        A("")

    memory = m.get("memoryColors") or []
    if memory:
        feats_mem = (m.get("features") or {}).get("memoryColors") or {}
        A("## 🎨 Памятные цвета")
        A("")
        # Чем найдена область — зависит от движка, и врать об этом в отчёте
        # нельзя: по нему пользователь решает, где искать виноватого.
        where = ("Photoshop (Select Sky, Select People, цветовой диапазон)"
                 if memory_engine(m["config"]) == "photoshop" else
                 "модель разбора человека (кожа), Apple Vision и коробки Lab "
                 "(небо, зелень)")
        A("Кожа, небо и зелень — цвета, ошибку в которых видно без оригинала "
          "рядом. Область находит %s, числа считаются печатные — как область "
          "ляжет в `%s`. Правится только то, что вышло за норму, и не больше "
          "чем на %s%% краски; правка кладётся в исходное пространство файла, "
          "до цветоделения."
          % (where, m["config"]["target_icc"],
             feats_mem.get("maxShiftPercent") or 12))
        A("")
        A("| Файл | Стр. | Цвет | Доля кадра | Было CMYK | Стало CMYK | Что сделано |")
        A("|---|---|---|---|---|---|---|")
        for item in memory:
            for f in item.get("findings") or []:
                status = f.get("status")
                title = MEMORY_COLOR_TITLES.get(f.get("color"), f.get("color"))
                shifts = [x for x in (_parse_shift(v)
                                      for v in (f.get("shift") or "").split("|")) if x]
                what = {
                    "none": "область не найдена",
                    "small": f.get("why") or "область слишком мала",
                    "unlike": "по числам не %s — не трогаем" % title,
                    "ok": "✅ в норме",
                    "error": f.get("why") or "сбой",
                }.get(status)
                if what is None:
                    what = ("; ".join("%s %.0f → %.0f%% (%s = %s, норма %s %s)"
                                      % (x["channel"], x["from"], x["to"], x["label"],
                                         _fmt_num(x["value"]),
                                         "не выше" if x["value"] > x["bound"] else "не ниже",
                                         _fmt_num(x["bound"]))
                                      for x in shifts))
                    what = ("🎯 " if status == "corrected" else "план: ") + what
                A("| `%s` | %s | %s | %s | %s | %s | %s |"
                  % (Path(item["path"]).name, ", ".join(item.get("pages") or []) or "?",
                     title,
                     "%.1f%%" % f["area"] if f.get("area") else "—",
                     f.get("before") or "—", f.get("after") or "—", what))
        A("")

    low = m.get("needsAiUpscale") or []
    done = [r for r in low if r.get("aiUpscaled")]
    left = [r for r in low if not r.get("aiUpscaled")]

    if done:
        A("## 🔍 Прошли ИИ-апскейл — просмотреть глазами")
        A("")
        A("Модель додумывает детали. Особое внимание — мелкий текст, шильдики, "
          "логотипы, лица.")
        A("")
        A("| Файл | Было PPI | Было пикселей | Коэф. | Страницы |")
        A("|---|---|---|---|---|")
        for r in done:
            px = "%s×%s" % tuple(r["pixels"]) if r.get("pixels") else "—"
            A("| `%s` | %.0f | %s | ×%.2f | %s |"
              % (Path(r["path"]).name, r["minEffectivePpi"], px,
                 r["neededScale"], ", ".join(r["pages"])))
        A("")

    if left:
        A("## ⚠ Низкое разрешение, ИИ-апскейл не применён")
        A("")
        A("Ниже %s ppi. Нужен другой исходник или меньший фрейм."
          % m["config"].get("upscale_min_ppi"))
        A("")
        A("| Файл | Effective PPI | Пиксели | Нужен коэф. | Почему без ИИ | Страницы |")
        A("|---|---|---|---|---|---|")
        for r in left:
            px = "%s×%s" % tuple(r["pixels"]) if r.get("pixels") else "—"
            A("| `%s` | %.0f | %s | ×%.2f | %s | %s |"
              % (Path(r["path"]).name, r["minEffectivePpi"], px, r["neededScale"],
                 r.get("skipReason") or "—", ", ".join(r["pages"])))
        A("")

    if todo:
        A("## %s" % ("Будут обработаны" if m["dryRun"] else "Обработанные изображения"))
        A("")
        A("| Файл | Причина | Было | Стало | Effective PPI |")
        A("|---|---|---|---|---|")
        for cand in todo:
            r = cand.result or {}
            before = r.get("before") or cand.probe or {}
            after = r.get("after") or {}
            b = ("%sx%s %s" % (before.get("width"), before.get("height"), before.get("cs"))
                 if before else "—")
            a = ("%sx%s %s" % (after.get("width"), after.get("height"), after.get("cs"))
                 if after else r.get("status", "—"))
            eff = "%.0f → %s" % (
                cand.min_effective_ppi or 0,
                "%.0f" % min(r.get("new_effective_ppi", {}).values())
                if r.get("new_effective_ppi") else "—")
            A("| `%s` | %s | %s | %s | %s |"
              % (cand.path.name, "; ".join(say_all(cand.reasons, lang)), b, a, eff))
        A("")

    if compliant:
        A("## Соответствуют требованиям (не тронуты)")
        A("")
        for cand in compliant:
            A("- `%s` — %s, min effective PPI %s"
              % (cand.path.name, cand.probe["cs"],
                 "%.0f" % cand.min_effective_ppi if cand.min_effective_ppi else "?"))
        A("")

    if skipped:
        A("## Не рассматривались")
        A("")
        for name, why in skipped:
            A("- `%s` — %s" % (name, why))
        A("")

    path.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- #
#  Настройки для панели в Prepress.app
# --------------------------------------------------------------------------- #
#
# Панель настроек живёт в дроплете (AppleScript), но ничего не знает про JSON:
# формат конфига разбирается и пишется только здесь. AppleScript получает
# готовые строки для показа (--config-menu) и присылает обратно одно
# «ключ=значение» (--config-set). Так вся валидация остаётся в одном месте
# и её видно в тестах, а панель остаётся тонкой.
#
# В панель вынесено не всё, а то, что реально меняют от заказа к заказу.
# Остальное правится в config.json руками — иначе панель превращается во
# второй способ сломать конфиг.

# Поле: (ключ, раздел, подпись, тип, дополнение)
#   тип "bool"   — переключается по месту;
#   тип "number" — ввод числа, дополнение = (минимум, максимум);
#   тип "text"   — ввод строки;
#   тип "choice" — выбор из списка, дополнение = [(значение, подпись), ...];
#   тип "file"   — выбор файла, дополнение = расширение;
#   тип "mode"   — две связанные булевы настройки, см. _MODE_KEYS.
# Куда ложится результат и что при этом станет с исходником. В конфиге это
# по-прежнему две настройки (files_output и originals_mode), но ручка ОДНА:
# порознь они допускали невозможное — «поверх исходников» вместе с «перенести
# оригинал в originals/». Пайплайн такую пару и не исполнял: перед тем как
# встать на место исходника, файл уезжает в originals/, то есть «без возврата»
# оказывалось ложью, а при keep запись поверх вообще отменялась и результат
# молча получал суффикс.
#
# Оставлены только достижимые исходы. «copy» из панели убран намеренно:
# при записи на место исходника он даёт ровно то же, что «move», а в двух
# других режимах исходник и так цел — копия была бы дубликатом живого файла.
_PLACEMENT_KEY = "output_placement"
_PLACEMENT_CHOICES = [
    ("folder", "в отдельную папку рядом, исходники не трогать"),
    ("suffix", "рядом с исходником, с суффиксом"),
    ("replace", "на место исходника, оригинал — в originals/"),
]


def _placement_of(cfg):
    mode = str(cfg.get("files_output") or "folder").strip().lower()
    return "replace" if mode == "in_place" else (
        "folder" if mode == "folder" else "suffix")


# Какие точки правим. Держится на двух ключах, но ручка ОДНА: серая точка
# без белой бессмысленна, а две галочки допускали бы такое сочетание.
_POINTS_KEY = "global_tone.points"
_POINTS_CHOICES = [("white", "точка белого"),
                   ("white_grey", "точка белого и серого")]

SETTINGS_FIELDS = [
    # Цепочка первой: она решает, какие из остальных настроек вообще
    # что-то значат в этом прогоне.
    ("pipeline", "Цепочка", "Что делаем", "choice", "__pipelines__"),
    ("report_lang", "Цепочка", "Язык отчёта", "choice",
     [("ru", "Русский"), ("en", "English"), ("system", "Как в системе")]),

    # Общий выключатель движка, и от него зависят три строки ниже.
    ("engine", "Цвет", "Чем обрабатывать растр", "choice",
     [("photoshop", "вариант 1: всё на Adobe"),
      ("native", "вариант 2: без Adobe (magick + Apple)")]),
    ("target_icc", "Цвет", "Профиль CMYK (цветные)", "file", "icc"),
    ("grayscale_icc", "Цвет", "Профиль CMYK (серые)", "file", "icc"),
    ("convert_grayscale_to_cmyk", "Цвет", "Серые делить в CMYK", "bool", None),
    ("color_engine", "Цвет", "Чем делить цвет", "choice",
     [("auto", "как выбрано выше"), ("magick", "командная строка (magick)"),
      ("photoshop", "Adobe Photoshop")]),

    ("max_effective_ppi", "Разрешение", "Уменьшать выше, ppi", "number", (72, 2400)),
    ("target_effective_ppi", "Разрешение", "Целевое разрешение, ppi", "number", (72, 1200)),
    ("upscale_below_ppi", "Разрешение", "Увеличивать ниже, ppi", "number", (72, 1200)),
    ("upscale_min_ppi", "Разрешение", "Ниже — ИИ-апскейл, ppi", "number", (0, 1200)),
    ("ai_upscale.enabled", "Разрешение", "ИИ-апскейл (Real-ESRGAN)", "bool", None),

    ("bleed_required_mm", "Вылеты", "Требуемый вылет, мм", "number", (0, 50)),
    ("bleed_scope", "Вылеты", "Считать от", "choice",
     [("spread", "разворота"), ("page", "страницы")]),

    ("text_in_links.mode", "Связи", "Живой текст в .ai/.eps/.pdf", "mode", None),
    ("spot_colors.mode", "Связи", "Плашки (Pantone)", "mode", None),

    ("master.enabled", "Файлы", "Откладывать мастер для планшета", "bool", None),

    ("crop_to_frame.enabled", "Обрезка", "Обрезать по фрейму", "bool", None),
    ("crop_to_frame.margin_mm", "Обрезка", "Запас вокруг фрейма, мм", "number", (0, 100)),
    ("crop_to_frame.scale_to_100", "Обрезка", "Масштаб в макете ровно 100%", "bool", None),

    ("global_tone.mode", "Памятные цвета", "Общий тон (точки и экспозиция)", "mode", None),
    ("memory_colors.mode", "Памятные цвета", "Кожа, небо, зелень", "mode", None),
    ("memory_colors.max_shift_percent", "Памятные цвета",
     "Предел правки, % краски", "number", (1, 40)),
    ("memory_colors.engine", "Памятные цвета", "Чем искать области", "choice",
     [("auto", "как выбрано в «Цвет»"), ("photoshop", "Adobe Photoshop"),
      ("native", "Apple Vision + magick")]),
    ("lift.mode", "Памятные цвета", "Вытягивать недодержанные кадры", "mode", None),
    ("lift.skin_target", "Памятные цвета", "Норма яркости кожи", "number", (0.1, 0.9)),
    ("lift.max_exposure", "Памятные цвета", "Предел экспозиции", "number", (1.0, 3.0)),
    ("lift.max_saturation", "Памятные цвета", "Подъём насыщенности, %", "number", (0, 60)),
    (_POINTS_KEY, "Памятные цвета", "Какие точки правим", "choice", _POINTS_CHOICES),
    ("global_tone.engine", "Памятные цвета", "Чем править общий тон", "choice",
     [("auto", "как выбрано в «Цвет»"), ("own", "own arithmetic (Photoshop)"),
      ("photoshop", "Photoshop auto levels"), ("coreimage", "Apple Core Image")]),
    ("memory_colors.skin.enabled", "Памятные цвета", "Кожа", "bool", None),
    ("memory_colors.sky.enabled", "Памятные цвета", "Небо", "bool", None),
    ("memory_colors.grass.enabled", "Памятные цвета", "Зелень", "bool", None),

    ("sharpen.enabled", "Резкость", "Шарпинг", "bool", None),
    ("sharpen.unsharp", "Резкость", "Параметры unsharp", "text", None),

    (_PLACEMENT_KEY, "Файлы", "Куда писать результат", "choice",
     _PLACEMENT_CHOICES),
    ("output_document.save_as_new", "Файлы", "Сохранять в новый .indd", "bool", None),
    ("output_document.suffix", "Файлы", "Суффикс нового .indd", "text", None),
    ("output_document.image_suffix", "Файлы", "Постоянный суффикс картинки",
     "text", None),
    ("output_document.image_suffixes.to_cmyk", "Файлы",
     "Суффикс имени: перевод в CMYK", "text", None),
    ("output_document.image_suffixes.to_rgb", "Файлы",
     "Суффикс имени: перевод в RGB", "text", None),
    ("output_document.image_suffixes.corrected", "Файлы",
     "Суффикс имени: только правка цвета", "text", None),
    ("output_document.image_suffixes.other", "Файлы",
     "Суффикс имени: цвет не менялся", "text", None),

    ("output_format", "Файлы", "Формат картинок на выходе", "choice",
     [("jpeg", "JPEG"), ("tiff", "TIFF (без потерь)")]),
    ("jpeg_quality", "Файлы", "Качество JPEG", "number", (1, 100)),

    # Настройка чужого инструмента (export_pdf.py), но панель в проекте одна,
    # и заводить вторую ради одной строки было бы хуже, чем показать её здесь.
    ("pdf_export.preset", "Экспорт PDF", "Пресет экспорта", "text", None),
]

# Виртуальные поля «выключено / только проверка / проверка и перевод»:
# в конфиге это две булевы настройки, но по отдельности они путают —
# «перевод включён, а проверка выключена» ничего не значит.
_MODE_KEYS = {
    "text_in_links.mode": ("text_in_links.enabled", "text_in_links.convert_to_outlines"),
    "spot_colors.mode": ("spot_colors.enabled", "spot_colors.convert_to_cmyk"),
    "memory_colors.mode": ("memory_colors.enabled", "memory_colors.apply"),
    "global_tone.mode": ("global_tone.enabled", "global_tone.apply"),
    "lift.mode": ("lift.enabled", "lift.apply"),
}
_MODE_CHOICES = [("off", "выключено"),
                 ("warn", "только проверка и предупреждение"),
                 ("full", "проверка и автоперевод")]

# У памятных цветов те же три ступени, но «проверка» — это замер чисел,
# а «перевод» — правка кривыми. Подписи общего вида сбивали бы с толку.
_MODE_CHOICES_BY_KEY = {
    "global_tone.mode": [("off", "выключено"),
                         ("warn", "только замер и отчёт"),
                         ("full", "замер и правка")],
    "memory_colors.mode": [("off", "выключено"),
                           ("warn", "только замер и отчёт"),
                           ("full", "замер и правка")],
    "lift.mode": [("off", "выключено"),
                  ("warn", "только замер и отчёт"),
                  ("full", "замер и правка")],
}


def _mode_choices(key):
    return _MODE_CHOICES_BY_KEY.get(key, _MODE_CHOICES)


def _dig(cfg, dotted):
    """Значение по пути «секция.ключ» или None."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _plant(cfg, dotted, value):
    """Записать значение по пути «секция.ключ», создавая секции по дороге."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ValueError("%s is not a settings section" % part)
    node[parts[-1]] = value


def read_config_file(path):
    """Конфиг как есть, вместе с ключами-комментариями: их надо сохранить."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _blank_line_keys(text):
    """
    Ключи верхнего уровня, перед которыми в файле стоит пустая строка.

    json.dump такую разметку не сохраняет, а она делает конфиг читаемым —
    поэтому запоминаем её и восстанавливаем после записи.
    """
    keys, blank = set(), False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            blank = True
            continue
        m = re.match(r'^  "([^"]+)"\s*:', line)
        if m and blank:
            keys.add(m.group(1))
        blank = False
    return keys


def write_config_file(path, cfg, blank_before=()):
    """Записывает конфиг, возвращая на место пустые строки между разделами."""
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    if blank_before:
        out = []
        for line in text.splitlines():
            m = re.match(r'^  "([^"]+)"\s*:', line)
            if m and m.group(1) in blank_before and out:
                out.append("")
            out.append(line)
        text = "\n".join(out)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _mode_of(cfg, dotted):
    enabled_key, convert_key = _MODE_KEYS[dotted]
    if not _dig(cfg, enabled_key):
        return "off"
    return "full" if _dig(cfg, convert_key) else "warn"


def _display(cfg, key, kind, extra):
    """Как значение показать в панели."""
    if kind == "mode":
        mode = _mode_of(cfg, key)
        return dict(_mode_choices(key))[mode]
    if key == _PLACEMENT_KEY:
        return dict(_PLACEMENT_CHOICES)[_placement_of(cfg)]
    if key == _POINTS_KEY:
        return dict(_POINTS_CHOICES)[
            "white_grey" if (cfg.get("global_tone") or {}).get("gray_point")
            else "white"]
    name = _stage_key(key)
    if name:
        return "да" if name in [stage_id(x) for x in _current_chain(cfg)[1]] else "нет"
    value = _dig(cfg, key)
    if kind == "bool":
        return "да" if value else "нет"
    if kind == "choice":
        # Список вариантов может быть не литералом, а именем источника
        # (см. "__pipelines__"): тогда показываем значение как есть.
        if not isinstance(extra, (list, tuple)):
            return str(value)
        return dict(extra).get(str(value), str(value))
    if kind == "file":
        return Path(str(value)).name if value else "не задан"
    if kind == "number":
        num = float(value or 0)
        return "%g" % num
    return str(value if value is not None else "")


REPORT_LANGS = ("ru", "en")


def report_lang(cfg):
    """
    Язык отчёта и журнала. "system" — по языку системы.

    Отдельно от языка интерфейса приложения намеренно: отчёт уходит наружу,
    заказчику, и его язык — свойство работы, а не того, кто её запустил.
    """
    value = str(cfg.get("report_lang") or "ru").strip().lower()
    if value == "system":
        import locale
        code = (locale.getdefaultlocale()[0] or "ru")[:2].lower()
        return code if code in REPORT_LANGS else "en"
    return value if value in REPORT_LANGS else "ru"


# Строки, которые уходят в GUI: подтверждение прогона и подписи настроек.
# Они единственные двуязычные в пайплайне — всё остальное «под капотом»
# по-английски. Язык приходит параметром: решает тот, кто показывает.
GUI_STR = {
"en": {
  "pasteboard": "images left on the pasteboard will be deleted",
  "crop": "images will be cropped to the frame", "crop100": ", scale set to 100%",
  "upscale": "small images will be enlarged by AI",
  "lift": "underexposed frames will be lifted out of the dark",
  "colour": "colour will be corrected: ", "tone": "overall tone", "memory": "memory colours",
  "and": " and ", "to_cmyk": "images will become CMYK %s",
  "to_rgb": "images will become RGB (%s)", "cs_same": "the colour space will not change",
  "resample": "resolution will be brought to %g ppi", "sharpen": "sharpening will be applied",
  "outline": "live text in vector links will be outlined",
  "relink": "links will be relinked",
  "save_as": "the layout will be saved to a new file with suffix %s",
  "out_folder": "output: a separate folder next to the source",
  "out_suffix": "output: next to the source, with a suffix",
  "out_inplace": "output: in place of the sources, originals go to originals/",
  "out_inplace_nokeep": "output: OVERWRITING the sources — no undo",
  "stages": "Stages", "chain": "Chain",
},
"ru": {
  "pasteboard": "картинки с полей удалятся",
  "crop": "картинки обрежутся по фрейму", "crop100": ", масштаб станет 100%",
  "upscale": "мелкие картинки увеличит ИИ",
  "lift": "недодержанные кадры вытянутся из темноты",
  "colour": "поправится цвет: ", "tone": "общий тон", "memory": "памятные цвета",
  "and": " и ", "to_cmyk": "картинки станут CMYK %s",
  "to_rgb": "картинки станут RGB (%s)", "cs_same": "цветовое пространство не меняется",
  "resample": "разрешение приведётся к %g ppi", "sharpen": "наведётся резкость",
  "outline": "текст в векторных связях станет кривыми",
  "relink": "связи переподключатся",
  "save_as": "вёрстка сохранится в новый файл с суффиксом %s",
  "out_folder": "результат: отдельная папка рядом с исходной",
  "out_suffix": "результат: рядом с исходником, с суффиксом",
  "out_inplace": "результат: на место исходников, оригиналы в originals/",
  "out_inplace_nokeep": "результат: ПОВЕРХ исходников — откатиться нечем",
  "stages": "Этапы", "chain": "Цепочка",
}}


def gui(lang, key, *args):
    s = GUI_STR.get(lang, GUI_STR["en"]).get(key) or GUI_STR["en"].get(key) or key
    return (s % args) if args else s


def run_info(path, lang=None, name=None, stages=None):
    """
    Что этот прогон сделает — строками для дроплета.

    Текст подтверждения ОБЯЗАН приходить отсюда, а не жить в AppleScript:
    зашитый в дроплете список («картинки станут CMYK TIFF, связи
    переподключатся») врал всякий раз, когда цепочку меняли, — а он и есть
    последнее, что человек читает перед необратимым прогоном.

    Первая строка говорит, ЧТО дроплету искать в брошенном: макеты или
    картинки. Это тоже решает цепочка: без этапа «связи» макет обрабатывать
    нечем, и собирать .indd бессмысленно.
    """
    cfg = read_config_file(path)
    lang = lang or report_lang(cfg)
    # Состав спрашивается ТЕМИ ЖЕ аргументами, с какими пойдёт прогон:
    # иначе окно подтверждения обещает одно, а делается другое. Раньше
    # строка бралась из конфига и в режиме картинок обещала обрезку
    # по фрейму, которой в прогоне не было.
    try:
        stages = resolve_pipeline(cfg, name=name, stages=stages,
                                  has_document=True)
    except RuntimeError:
        try:
            stages = resolve_pipeline(cfg, name=name, stages=stages,
                                      has_document=False)
        except RuntimeError:
            stages = ()
    kind = "layout" if "links" in stages else "images"

    lines = []
    if "pasteboard" in stages:
        lines.append(gui(lang, "pasteboard"))
    if "crop" in stages:
        lines.append(gui(lang, "crop") +
                     (gui(lang, "crop100") if want_scale_100(cfg) else ""))
    if "upscale" in stages:
        lines.append(gui(lang, "upscale"))
    if "lift" in stages:
        lines.append(gui(lang, "lift"))
    if "tone" in stages or "memory" in stages:
        what = gui(lang, "and").join(
            x for x in (gui(lang, "tone") if "tone" in stages else "",
                        gui(lang, "memory") if "memory" in stages else "") if x)
        lines.append(gui(lang, "colour") + what)
    if "convert" in stages:
        if color_direction(cfg) == "to_rgb":
            rgb = target_rgb_icc(cfg)
            lines.append(gui(lang, "to_rgb",
                             Path(rgb).stem if rgb else "RGB"))
        else:
            lines.append(gui(lang, "to_cmyk", output_format(cfg).upper()))
    else:
        lines.append(gui(lang, "cs_same"))
    if "resample" in stages:
        lines.append(gui(lang, "resample", float(cfg["target_effective_ppi"])))
    if "sharpen" in stages and (cfg.get("sharpen") or {}).get("enabled"):
        lines.append(gui(lang, "sharpen"))
    if "text" in stages and want_outlines(cfg):
        lines.append(gui(lang, "outline"))
    if "relink" in stages:
        lines.append(gui(lang, "relink"))
        if save_as_new_document(cfg):
            lines.append(gui(lang, "save_as", doc_suffix(cfg)))
    mode = files_output_mode(cfg)
    lines.append(gui(lang, "out_folder" if mode == "folder"
                     else "out_suffix" if mode == "suffix"
                     else "out_inplace_nokeep" if keep_originals_in_place(cfg)
                     else "out_inplace"))

    out = ["kind\t" + kind,
           "chain\t" + (" -> ".join(stages) if stages else "(пусто)")]
    out += ["line\t" + x for x in lines]
    return "\n".join(out)


# Подписи панели настроек на двух языках. Панель — это GUI (дроплет,
# приложение), поэтому русский здесь законен; всё остальное «под капотом»
# по-английски. Ключ словаря — русская подпись из SETTINGS_FIELDS: так
# добавить новую настройку можно, не трогая перевод, а непереведённая
# подпись просто остаётся русской и это видно глазом.
PANEL_EN = {
    # разделы
    "Цепочка": "Chain", "Цвет": "Colour", "Разрешение": "Resolution",
    "Вылеты": "Bleed", "Связи": "Links", "Файлы": "Files",
    "Обрезка": "Crop", "Памятные цвета": "Memory colours", "Резкость": "Sharpening",
    "Экспорт PDF": "PDF export", "Этапы": "Stages",
    # поля
    "Что делаем": "What we do",
    "Постоянный суффикс картинки": "Fixed image suffix",
    "Суффикс имени: перевод в CMYK": "Name suffix: converted to CMYK",
    "Суффикс имени: перевод в RGB": "Name suffix: converted to RGB",
    "Суффикс имени: только правка цвета": "Name suffix: colour corrected",
    "Суффикс имени: цвет не менялся": "Name suffix: colour unchanged",
    "Какие точки правим": "Which points to correct",
    "Норма яркости кожи": "Skin luminance norm",
    "Предел экспозиции": "Exposure limit",
    "Подъём насыщенности, %": "Saturation boost, %",
    "точка белого": "white point", "точка белого и серого": "white and grey point",
    "Язык отчёта": "Report language",
    "Русский": "Русский", "English": "English", "Как в системе": "Match system",
    "Куда писать результат": "Where to write the result",
    "в отдельную папку рядом, исходники не трогать":
        "into a separate folder, sources untouched",
    "на место исходника, оригинал — в originals/":
        "in place of the source, the original goes to originals/",
    "Чем обрабатывать растр": "What processes the raster",
    "Профиль CMYK (цветные)": "CMYK profile (colour)",
    "Профиль CMYK (серые)": "CMYK profile (grey)",
    "Серые делить в CMYK": "Separate grey into CMYK",
    "Чем делить цвет": "What separates colour",
    "Уменьшать выше, ppi": "Reduce above, ppi",
    "Целевое разрешение, ppi": "Target resolution, ppi",
    "Увеличивать ниже, ppi": "Enlarge below, ppi",
    "Ниже — ИИ-апскейл, ppi": "Below — AI upscale, ppi",
    "ИИ-апскейл (Real-ESRGAN)": "AI upscale (Real-ESRGAN)",
    "Требуемый вылет, мм": "Required bleed, mm",
    "Считать от": "Measure from",
    "Живой текст в .ai/.eps/.pdf": "Live text in .ai/.eps/.pdf",
    "Плашки (Pantone)": "Spot colours (Pantone)",
    "Откладывать мастер для планшета": "Keep a master for the tablet",
    "Обрезать по фрейму": "Crop to frame",
    "Запас вокруг фрейма, мм": "Margin around the frame, mm",
    "Масштаб в макете ровно 100%": "Scale in the layout exactly 100%",
    "Общий тон (точки и экспозиция)": "Overall tone (points and exposure)",
    "Кожа, небо, зелень": "Skin, sky, greenery",
    "Предел правки, % краски": "Correction limit, % ink",
    "Чем искать области": "What finds the regions",
    "Чем править общий тон": "What corrects the overall tone",
    "Кожа": "Skin", "Небо": "Sky", "Зелень": "Greenery",
    "Шарпинг": "Sharpening", "Параметры unsharp": "Unsharp parameters",
    "Сохранять в новый .indd": "Save to a new .indd",
    "Суффикс нового .indd": "Suffix of the new .indd",
    "Исходные картинки": "Source images",
    "Формат картинок на выходе": "Output image format",
    "Качество JPEG": "JPEG quality",
    "Пресет экспорта": "Export preset",
    # этапы
    "Убирать картинки с полей": "Remove images from the pasteboard",
    "Брать список из InDesign (иначе вход — папка с картинками)":
        "Take the list from InDesign (otherwise the input is a folder of images)",
    "Проверять вылет под обрез": "Check the bleed past the trim",
    "Текст в векторных связях в кривые": "Outline text in vector links",
    "Плашки в процессные": "Spot colours to process",
    "ИИ-апскейл мелких картинок": "AI upscale for small images",
    "Вытягивать недодержанные кадры": "Lift underexposed frames",
    "Цветокоррекция: общий тон и точка белого":
        "Colour correction: overall tone and white point",
    "Цветокоррекция: кожа, небо, зелень":
        "Colour correction: skin, sky, greenery",
    "Переводить цветовое пространство": "Convert colour space",
    "Приводить разрешение": "Bring resolution to target",
    "Наводить резкость": "Apply sharpening",
    "Переподключать связи и сохранять вёрстку": "Relink and save the layout",
    # значения выбора
    "выключено": "off", "только проверка и предупреждение": "check and warn only",
    "проверка и автоперевод": "check and auto-convert",
    "только замер и отчёт": "measure and report only", "замер и правка": "measure and correct",
    "вариант 1: всё на Adobe": "option 1: everything on Adobe",
    "вариант 2: без Adobe (magick + Apple)": "option 2: without Adobe (magick + Apple)",
    "как выбрано выше": "as selected above",
    "командная строка (magick)": "command line (magick)",
    "Adobe Photoshop": "Adobe Photoshop",
    "в отдельную папку рядом": "into a separate folder alongside",
    "рядом с исходником, с суффиксом": "next to the source, with a suffix",
    "поверх исходников": "overwrite the sources",
    "своя арифметика": "own arithmetic", "авто-уровни Photoshop": "Photoshop auto levels",
    "Apple Core Image": "Apple Core Image",
    "Vision + magick": "Vision + magick",
    "да": "yes", "нет": "no", "не задан": "not set",
    "как выбрано в «Цвет»": "as selected in Colour",
    "Apple Vision + magick": "Apple Vision + magick",
    "own arithmetic (Photoshop)": "own arithmetic (Photoshop)",
    "оставлять на месте": "keep in place",
    "переносить в originals/": "move to originals/",
    "копировать в originals/": "copy to originals/",
    "TIFF (без потерь)": "TIFF (lossless)",
    "разворота": "spread", "страницы": "page",
    "оставить на месте": "keep in place", "перенести в originals": "move to originals",
    "копировать в originals": "copy to originals",
}


def panel_text(text, lang):
    """Подпись панели на нужном языке. Нет перевода — отдаём как есть."""
    if lang != "en":
        return text
    return PANEL_EN.get(text, text)


_SUFFIX_CASE_KEYS = tuple("output_document.image_suffixes." + n
                          for n in ("to_cmyk", "to_rgb", "corrected", "other"))


def _hidden_rows(cfg):
    """
    Строки, которых в панели быть не должно ПРИ ЭТОМ конфиге.

    Суффикс картинки живёт в двух видах, и оба сразу показывать нельзя.
    Обычный (`auto`) — собирается по тому, что с файлом сделали, и панели
    место в четырёх внятных строках, а не в поле, где написано слово «auto»
    и человеку нечего в него вписать. Постоянный суффикс, наоборот, делает
    эти четыре мёртвыми, и показывать их значило бы врать.

    Тупика здесь нет: постоянный суффикс из панели ставится одинаковым
    значением во всех четырёх строках — результат ровно тот же.
    """
    fixed = str(doc_output_opts(cfg).get("image_suffix")
                or "auto").strip() != "auto"
    hide = set(_SUFFIX_CASE_KEYS) if fixed else {"output_document.image_suffix"}

    # Суффикс имени вообще имеет смысл ровно в одном месте назначения —
    # «рядом с исходником». В отдельной папке имена сохраняются (в этом весь
    # смысл: папку отдают заказчику как есть), а на месте исходника результат
    # занимает его имя. Строка с суффиксом там обещала бы то, чего не будет.
    #
    # Совсем из жизни суффикс при этом не уходит: если по имени результат
    # столкнулся бы с чужим файлом, он всё равно достанется ему — но это
    # аварийный выход, а не настройка.
    if _placement_of(cfg) != "suffix":
        hide |= set(_SUFFIX_CASE_KEYS) | {"output_document.image_suffix"}
    return hide


def config_menu(path, lang=None):
    """
    Строки для панели настроек: раздел, подпись, значение, тип, варианты.

    Разделитель — табуляция: AppleScript разбирает такое в две строки кода,
    а в значениях настроек табуляции быть не может.
    """
    cfg = read_config_file(path)
    lang = lang or report_lang(cfg)
    rows = []
    hidden = _hidden_rows(cfg)
    for key, section, label, kind, extra in SETTINGS_FIELDS + _stage_fields():
        if key in hidden:
            continue
        section, label = panel_text(section, lang), panel_text(label, lang)
        if kind == "mode":
            options = ";".join("%s=%s" % (v, panel_text(t, lang))
                               for v, t in _mode_choices(key))
        elif kind == "choice":
            # Варианты могут жить в самом конфиге (цепочки): дописал цепочку
            # в pipelines — она появилась в панели, кода трогать не надо.
            extra = _choices(cfg, extra)
            options = ";".join("%s=%s" % (v, panel_text(t, lang)) for v, t in extra)
        elif kind == "number":
            options = "%g;%g" % extra
        elif kind == "file":
            options = str(extra or "")
        else:
            options = ""
        rows.append("\t".join([key, section, label,
                               panel_text(_display(cfg, key, kind, extra), lang),
                               kind, options]))
    return "\n".join(rows)


# Переключатели этапов — не настройки в обычном смысле: они правят СОСТАВ
# текущей цепочки в разделе pipelines. Отдельными ключами в config их нет,
# поэтому и живут они псевдо-именами «этап.<имя>». Заведены затем, чтобы
# способ выключить этап был ОДИН: раньше цветокоррекцию можно было снять
# и через global_tone.mode, и через состав цепочки — два рычага на одно
# действие рано или поздно стоят прогона.
STAGE_FIELD_PREFIX = "stage."
# Прежний префикс: панель дроплета и записанные команды пользуются им.
STAGE_FIELD_PREFIX_OLD = "этап."


def _stage_key(key):
    """Ключ переключателя -> имя этапа, или None. Понимает оба префикса."""
    for prefix in (STAGE_FIELD_PREFIX, STAGE_FIELD_PREFIX_OLD):
        if key.startswith(prefix):
            return stage_id(key[len(prefix):])
    return None

STAGE_FIELD_LABELS = {
    "pasteboard": "Убирать картинки с полей",
    "links": "Брать список из InDesign (иначе вход — папка с картинками)",
    "bleed": "Проверять вылет под обрез",
    "text": "Текст в векторных связях в кривые",
    "spots": "Плашки в процессные",
    "crop": "Обрезать по фрейму",
    "upscale": "ИИ-апскейл мелких картинок",
    "lift": "Вытягивать недодержанные кадры",
    "tone": "Цветокоррекция: общий тон и точка белого",
    "memory": "Цветокоррекция: кожа, небо, зелень",
    "master": "Откладывать мастер для планшета",
    "convert": "Переводить цветовое пространство",
    "resample": "Приводить разрешение",
    "sharpen": "Наводить резкость",
    "relink": "Переподключать связи и сохранять вёрстку",
}


# Прежние выключатели тех же действий. Этап включается ОДНОЙ ручкой, но
# в конфиге эти флаги остались и работают по «И» (см. check_crop, want_master,
# ai_available), так что выключенный флаг превратил бы включённый этап
# в молчаливое ничегонеделание. Поэтому переключатель этапа двигает и его.
_STAGE_LEGACY_FLAG = {
    "crop": "crop_to_frame.enabled",
    "upscale": "ai_upscale.enabled",
    "master": "master.enabled",
    "sharpen": "sharpen.enabled",
}


# Этапы, у которых в конфиге есть ещё и пара «enabled / apply», показанная
# как «только замер» / «замер и правка». Третий вариант этой пары —
# «выключено» — повторяет тумблер этапа, поэтому в интерфейсе его нет,
# и включение этапа обязано поднять enabled: иначе включённый тумблер
# наткнётся на enabled = false (все четыре работают по «И», см. check_text,
# check_spots, tone_spec, want_memory) и этап промолчит. Выключение флаг
# НЕ трогает: этапа нет в цепочке, этого достаточно, а сбросив enabled,
# мы стёрли бы выбор между замером и правкой.
_STAGE_MODE_FLAG = {
    "text": "text_in_links.enabled",
    "spots": "spot_colors.enabled",
    "lift": "lift.enabled",
    "tone": "global_tone.enabled",
    "memory": "memory_colors.enabled",
}


def _stage_fields():
    return [(STAGE_FIELD_PREFIX + name, "Этапы",
             STAGE_FIELD_LABELS.get(name, name), "bool", None)
            for name in STAGE_ORDER]


def _current_chain(cfg):
    """(имя цепочки, её состав списком). Цепочки нет — пустой список."""
    name = str(cfg.get("pipeline") or "")
    presets = pipeline_presets(cfg)
    return name, list(presets.get(name) or [])


def _field(key):
    for row in SETTINGS_FIELDS:
        if row[0] == key:
            return row
    name = _stage_key(key)
    if name and name in STAGE_INFO:
        return (key, "Этапы", STAGE_FIELD_LABELS.get(name, name), "bool", None)
    raise ValueError("unknown setting: %s" % key)


def _choices(cfg, extra):
    """Развернуть имя источника вариантов в сам список."""
    if extra == "__pipelines__":
        return [(name, name) for name in sorted(pipeline_presets(cfg))]
    return extra


def _coerce(key, kind, extra, raw):
    """Строка из панели -> значение нужного типа, с проверкой границ."""
    raw = str(raw).strip()
    if kind == "bool":
        low = raw.lower()
        if low in ("1", "true", "да", "yes", "on"):
            return True
        if low in ("0", "false", "нет", "no", "off"):
            return False
        raise ValueError("%s: expected yes/no, got %r" % (key, raw))
    if kind == "number":
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            raise ValueError("%s: %r is not a number" % (key, raw))
        lo, hi = extra
        if not (lo <= value <= hi):
            raise ValueError("%s: %g is outside the range %g..%g" % (key, value, lo, hi))
        return value
    if kind == "choice":
        allowed = [v for v, _ in extra]
        if raw not in allowed:
            raise ValueError("%s: %r is not in the list (%s)" % (key, raw, ", ".join(allowed)))
        return raw
    if kind == "mode":
        allowed = [v for v, _ in _mode_choices(key)]
        if raw not in allowed:
            raise ValueError("%s: %r is not in the list (%s)" % (key, raw, ", ".join(allowed)))
        return raw
    if kind == "file":
        if not Path(raw).expanduser().exists() and resolve_icc(raw) is None:
            raise ValueError("%s: file not found: %s" % (key, raw))
        return raw
    if not raw:
        raise ValueError("%s: empty value" % key)
    return raw


# Настройки связаны между собой, и панель обязана ловить это до записи:
# «целевое разрешение выше порога уменьшения» тихо ломает весь пайплайн.
def validate_config(cfg):
    """Список претензий к конфигу; пустой список — всё в порядке."""
    problems = []
    mx = float(cfg.get("max_effective_ppi") or 0)
    tg = float(cfg.get("target_effective_ppi") or 0)
    below = float(cfg.get("upscale_below_ppi") or 0)
    mn = float(cfg.get("upscale_min_ppi") or 0)

    if tg > mx:
        problems.append("the target resolution (%g) is above the downsample threshold (%g): "
                        "there would be nothing to downsample to" % (tg, mx))
    if below > mx:
        problems.append("the enlarge threshold (%g) is above the downsample threshold (%g): "
                        "the bands overlap" % (below, mx))
    if mn and below and mn > below:
        problems.append("the AI-upscale threshold (%g) is above the enlarge threshold (%g)" % (mn, below))
    if below and tg < below:
        problems.append("the target resolution (%g) is below the enlarge threshold (%g): "
                        "an enlarged image would count as weak again" % (tg, below))
    if cfg.get("output_document", {}).get("save_as_new"):
        suffix = cfg.get("output_document", {}).get("suffix") or ""
        if not suffix.strip():
            problems.append("saving to a new .indd is on, but the name suffix is empty")
    if str(cfg.get("originals_mode", "")).lower() not in ("keep", "move", "copy"):
        problems.append("originals_mode must be keep, move or copy")
    # «auto» — законное значение с тех пор, как появился общий выключатель
    # engine: этап берёт движок оттуда. Проверка про него не знала, и панель
    # настроек из-за этого отказывалась сохранять ЛЮБУЮ настройку, потому
    # что validate_config зовётся на весь конфиг целиком.
    if str(cfg.get("color_engine", "magick")).lower() not in COLOR_ENGINES + ("auto",):
        problems.append("color_engine must be auto, %s"
                        % " or ".join(COLOR_ENGINES))
    if str(cfg.get("output_format", "jpeg")).lower() not in OUTPUT_FORMATS:
        problems.append("output_format must be %s" % " or ".join(OUTPUT_FORMATS))
    try:
        q = float(cfg.get("jpeg_quality", 100))
    except (TypeError, ValueError):
        q = -1
    if not (1 <= q <= 100):
        problems.append("jpeg_quality must be between 1 and 100")
    crop = cfg.get("crop_to_frame") or {}
    if crop.get("enabled"):
        try:
            margin = float(crop.get("margin_mm", 10))
        except (TypeError, ValueError):
            margin = -1
        if not (0 <= margin <= 100):
            problems.append("crop_to_frame.margin_mm must be between 0 and 100 mm")
        try:
            saving = float(crop.get("min_saving_percent", 3))
        except (TypeError, ValueError):
            saving = -1
        if not (0 <= saving <= 90):
            problems.append("crop_to_frame.min_saving_percent must be between 0 and 90")

    mem = cfg.get("memory_colors") or {}
    if mem.get("enabled"):
        try:
            shift = float(mem.get("max_shift_percent", 12))
        except (TypeError, ValueError):
            shift = -1
        if not (0 < shift <= 40):
            problems.append("memory_colors.max_shift_percent must be between 1 and 40: "
                            "moving more than 40% of ink is no longer a correction")
        for name in MEMORY_COLOR_ORDER:
            section = mem.get(name) or {}
            for key in ("c_of_m", "y_minus_m", "m_of_c", "y_of_c", "m_range", "c_range"):
                pair = section.get(key)
                if pair is None:
                    continue
                if (not isinstance(pair, list) or len(pair) != 2
                        or (pair[0] is not None and pair[1] is not None
                            and float(pair[0]) > float(pair[1]))):
                    problems.append("memory_colors.%s.%s must be a pair "
                                    "[minimum, maximum]" % (name, key))
    if not str(cfg.get("pdf_export", {}).get("preset") or "").strip():
        problems.append("pdf_export.preset is empty — the PDF export has nothing to work with")
    return problems


def config_set(path, assignment):
    """
    Применяет одно «ключ=значение» из панели настроек.

    Возвращает текст для показа пользователю. Ошибка — исключение ValueError:
    конфиг при этом не трогается вовсе.
    """
    if "=" not in assignment:
        raise ValueError("expected key=value, got: %s" % assignment)
    key, raw = assignment.split("=", 1)
    key = key.strip()
    _, _, label, kind, extra = _field(key)

    original = Path(path).read_text(encoding="utf-8")
    cfg = json.loads(original)
    # Список вариантов может жить в самом конфиге (цепочки), поэтому
    # разворачиваем его ПОСЛЕ чтения файла и только потом проверяем значение.
    extra = _choices(cfg, extra)
    value = _coerce(key, kind, extra, raw)

    if key == _PLACEMENT_KEY:
        _plant(cfg, "files_output",
               "in_place" if value == "replace" else value)
        # Оригинал уезжает в originals/ ровно в одном случае — когда
        # результат занимает его место. В остальных он цел и так.
        _plant(cfg, "originals_mode", "move" if value == "replace" else "keep")
        shown = dict(_PLACEMENT_CHOICES)[value]
        stage_name = None
    elif key == _POINTS_KEY:
        # Белая точка включается всегда: без неё править нечего, а серая
        # сама по себе бессмысленна.
        _plant(cfg, "global_tone.neutral_points", True)
        _plant(cfg, "global_tone.gray_point", value == "white_grey")
        shown = dict(_POINTS_CHOICES)[value]
        stage_name = None
    else:
        stage_name = _stage_key(key)
    if stage_name:
        name = stage_name
        chain_name, chain = _current_chain(cfg)
        if not chain_name:
            raise ValueError("no chain selected (the pipeline setting)")
        want = set(chain)
        if value:
            want.add(name)
        else:
            want.discard(name)
        # Порядок не спрашиваем — он задан реестром; пишем состав в нём.
        new_chain = [x for x in STAGE_ORDER if x in want]
        cfg.setdefault("pipelines", {})[chain_name] = new_chain
        legacy = _STAGE_LEGACY_FLAG.get(name)
        if legacy:
            _plant(cfg, legacy, bool(value))
        if value and name in _STAGE_MODE_FLAG:
            _plant(cfg, _STAGE_MODE_FLAG[name], True)
        # Состав проверяем ТУТ ЖЕ: недопустимое сочетание не должно уехать
        # в конфиг и всплыть на середине боевого прогона.
        probe = dict(cfg)
        try:
            resolve_pipeline(probe, name=chain_name,
                             has_document=("links" in want))
        except RuntimeError as exc:
            raise ValueError("the chain does not work that way:\n• %s" % exc)
        shown = "да" if value else "нет"
    elif kind == "mode":
        enabled_key, convert_key = _MODE_KEYS[key]
        _plant(cfg, enabled_key, value != "off")
        _plant(cfg, convert_key, value == "full")
        shown = dict(_mode_choices(key))[value]
    else:
        _plant(cfg, key, value)
        shown = _display(cfg, key, kind, extra)

    problems = validate_config(cfg)
    if problems:
        raise ValueError("the setting was not saved — the pipeline does not work that way:\n• "
                         + "\n• ".join(problems))

    write_config_file(path, cfg, _blank_line_keys(original))
    return "%s: %s" % (label, shown)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        description="Prepress processing of InDesign links "
                    "(CMYK, downsampling, TIFF) with a full log.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  ./prepress.py '/path/doc.indd'\n"
               "  ./prepress.py --active --dry-run\n"
               "  ./prepress.py --pipeline color '/path/to/photos'\n"
               "  ./prepress.py --max-ppi 400 --target-ppi 300 '/path/doc.indd'\n")
    p.add_argument("documents", nargs="*",
                   help="Paths to .indd files, a folder, or image files")
    p.add_argument("--active", action="store_true",
                   help="Process the active InDesign document")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    p.add_argument("--pipeline", metavar="NAME",
                   help="Which chain of stages to run (names live in config.json, "
                        "section pipelines). Defaults to the pipeline setting.")
    p.add_argument("--stages", metavar="LIST",
                   help="Chain contents as a comma-separated list instead of a preset. "
                        "The order is fixed in the script and not configurable: "
                        + ", ".join(STAGE_ORDER))
    p.add_argument("--json-events", action="store_true",
                   help="Print run events as JSON lines on stdout (for the app). "
                        "The log is written as usual.")
    p.add_argument("--run-info", action="store_true",
                   help="What this run will do with the current settings: lines "
                        "for the GUI (what to accept and what to confirm)")
    p.add_argument("--lang", choices=("ru", "en"),
                   help="Language of everything a human reads in this run: the "
                        "report, --run-info and --config-menu. The log is always "
                        "English. Defaults to report_lang from config.json.")
    p.add_argument("--list-stages", action="store_true",
                   help="List stages and ready-made chains, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="Analysis and log only; no file or document is changed")
    p.add_argument("--no-relink", action="store_true",
                   help="Process the files but do not relink them in InDesign")
    p.add_argument("--no-save", action="store_true",
                   help="Relink but do not save the document")
    p.add_argument("--color-engine", choices=COLOR_ENGINES,
                   help="What separates colour: magick (command line) or photoshop")
    p.add_argument("--output-format", choices=OUTPUT_FORMATS,
                   help="What to write images as: jpeg or tiff")
    p.add_argument("--jpeg-quality", type=int,
                   help="JPEG quality, 1-100 (default 100)")
    p.add_argument("--profile", help="Override the target ICC profile")
    p.add_argument("--gray-profile", help="Override the ICC profile for Grayscale")
    p.add_argument("--max-ppi", type=float, help="Effective PPI threshold (default 350)")
    p.add_argument("--target-ppi", type=float, help="Target effective PPI (default 300)")
    p.add_argument("--sharpen", action="store_true",
                   help="Enable sharpening (currently a stub, see sharpen_args)")
    p.add_argument("--copy-originals", action="store_true",
                   help="Copy originals into originals/ instead of moving them")
    p.add_argument("--in-place", action="store_true",
                   help="Save changes into the same .indd instead of a new file "
                        "with the _CMYK suffix")
    p.add_argument("--no-outline-text", action="store_true",
                   help="Do not outline live text in vector links — warn only")
    p.add_argument("--no-color-correct", action="store_true",
                   help="Do not correct memory colours (skin, sky, greenery) — "
                        "measure only and record the numbers in the report")
    p.add_argument("--no-spot-convert", action="store_true",
                   help="Do not convert spot colours to CMYK — warn only. Raster "
                        "files with spots are then not processed at all")
    p.add_argument("--no-open", action="store_true",
                   help="Do not open the report when finished")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    # Internal, for the settings panel in the app; no need to call by hand.
    p.add_argument("--config-menu", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--config-set", metavar="KEY=VALUE",
                   help=argparse.SUPPRESS)
    return p


def _modal_dialog_blocked(exc):
    """
    Похоже ли, что дальше работать бессмысленно: InDesign занят модальным окном.

    Текст приходит от самого InDesign через osascript, поэтому опознаётся
    подстрокой. Локализованная сборка отдаёт свою формулировку, поэтому
    проверяются обе.
    """
    text = str(exc).lower()
    return "modal dialog or alert is active" in text or "модальн" in text


def main(argv=None):
    args = build_parser().parse_args(argv)

    # Панель настроек: работаем с файлом конфига и выходим, InDesign не нужен.
    if args.config_menu:
        print(config_menu(args.config, args.lang))
        return 0
    if args.config_set:
        try:
            print(config_set(args.config, args.config_set))
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    global EVENTS_ON
    EVENTS_ON = bool(args.json_events)

    if args.run_info:
        print(run_info(args.config, args.lang, args.pipeline,
                       [x.strip() for x in args.stages.split(",")]
                       if args.stages else None))
        return 0

    cfg_early = None
    if args.list_stages:
        cfg_early = load_config(args.config)
        print("Stages (the order is fixed; only the contents are configurable):")
        for name, scope, deps, title in PIPELINE_STAGES:
            print("  %-16s %-9s %s%s"
                  % (name, "layout" if scope == "document" else "image", title,
                     "" if not deps else "  [needs %s]" % ", ".join(deps)))
        print("")
        print("Ready-made chains (config.json, section pipelines):")
        for name, items in sorted(pipeline_presets(cfg_early).items()):
            mark = " <- default" if name == str(cfg_early.get("pipeline") or "") else ""
            print("  %-12s %s%s" % (name, " -> ".join(items), mark))
        return 0

    if not args.documents and not args.active:
        build_parser().error("give a path to an .indd, a folder, or use --active")

    cfg = load_config(args.config)
    if args.lang:
        # Перекрывает report_lang на этот запуск: приложение знает, на каком
        # языке говорит с человеком, а config.json — нет. Настройка в файле
        # остаётся прежней, меняется только этот прогон.
        cfg["report_lang"] = args.lang
    if args.pipeline:
        cfg["pipeline"] = args.pipeline
    if args.color_engine:
        cfg["color_engine"] = args.color_engine
    if args.output_format:
        cfg["output_format"] = args.output_format
    if args.jpeg_quality is not None:
        cfg["jpeg_quality"] = args.jpeg_quality
    if args.profile:
        cfg["target_icc"] = args.profile
    if args.gray_profile:
        cfg["grayscale_icc"] = args.gray_profile
    if args.max_ppi:
        cfg["max_effective_ppi"] = args.max_ppi
    if args.target_ppi:
        cfg["target_effective_ppi"] = args.target_ppi
    if args.sharpen:
        cfg.setdefault("sharpen", {})["enabled"] = True
    if args.copy_originals:
        cfg["originals_mode"] = "copy"
    if args.in_place:
        cfg.setdefault("output_document", {})["save_as_new"] = False
    if args.no_outline_text:
        cfg.setdefault("text_in_links", {})["convert_to_outlines"] = False
    if args.no_spot_convert:
        cfg.setdefault("spot_colors", {})["convert_to_cmyk"] = False
    if args.no_color_correct:
        cfg.setdefault("memory_colors", {})["apply"] = False
    if args.no_open:
        cfg["open_report_on_finish"] = False

    # Проверки окружения до того, как что-то трогать
    magick = resolve_magick(cfg)
    if magick is None:
        print("ImageMagick not found (tried: %s, PATH, /opt/homebrew/bin, /usr/local/bin).\n"
              "Install it: brew install imagemagick" % cfg.get("magick"),
              file=sys.stderr)
        return 2
    cfg["magick"] = magick
    for key in ("target_icc", "grayscale_icc", "assume_rgb_icc",
                "assume_cmyk_icc", "assume_gray_icc"):
        if not cfg.get(key):
            continue
        found = resolve_icc(cfg[key])
        if found is None:
            print("ICC profile not found (%s): %s\n"
                  "Check the path in config.json or put the profile "
                  "into /Library/ColorSync/Profiles/" % (key, cfg[key]), file=sys.stderr)
            return 2
        cfg[key] = str(found)

    # Что на входе: макеты или картинки. Смешивать нельзя — у них разные
    # цепочки и разные правила записи, и молча выбрать одну из двух хуже,
    # чем сказать об этом.
    layouts = [t for t in (args.documents or [])
               if Path(t).suffix.lower() == ".indd"]
    others = [t for t in (args.documents or []) if t not in layouts]
    if layouts and others:
        print("Both layouts and images on input — that will not work: they use "
              "different chains and different write rules. Run them separately.",
              file=sys.stderr)
        return 2

    if others:
        files = gather_image_files(cfg, others)
        if not files:
            print("No images found (extensions from raster_extensions): %s"
                  % ", ".join(others), file=sys.stderr)
            return 2
        # Цепочку пересобираем: этапы макета здесь невозможны, и сказать
        # об этом надо до того, как что-то тронуто.
        try:
            cfg["__stages"] = resolve_pipeline(
                cfg, name=args.pipeline,
                stages=[x.strip() for x in args.stages.split(",")] if args.stages else None,
                has_document=False)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            print("For a folder of photos use the colour chain: "
                  "--pipeline color", file=sys.stderr)
            return 2
        try:
            process_document(cfg, None, args, files=files)
            return 0
        except Exception as exc:
            if log.handlers:
                log.exception("FAILED to process the folder: %s", exc)
            else:
                print("FAILED: %s" % exc, file=sys.stderr)
            return 1

    # Цепочка для макета. Разбирается до всякой работы: ошибка в составе
    # должна стоить нисколько, а не половину прогона.
    try:
        cfg["__stages"] = resolve_pipeline(
            cfg, name=args.pipeline,
            stages=[x.strip() for x in args.stages.split(",")] if args.stages else None,
            has_document=True)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print("What exists: ./prepress.py --list-stages", file=sys.stderr)
        return 2

    targets = layouts or [None]
    failures = 0
    for n_done, doc in enumerate(targets):
        if doc is not None and not Path(doc).exists():
            print("File not found: %s" % doc, file=sys.stderr)
            failures += 1
            continue
        try:
            process_document(cfg, str(Path(doc).resolve()) if doc else None, args)
        except Exception as exc:
            failures += 1
            if log.handlers:
                log.exception("FAILED to process %s: %s", doc or "the active document", exc)
            else:
                print("FAILED: %s" % exc, file=sys.stderr)
            # Модальное окно в InDesign валит не один макет, а ВСЕ следующие:
            # приложение перестаёт принимать команды вовсе, и остаток пачки
            # отрабатывает с той же ошибкой за секунду. Замер на номере из 11
            # папок: окно поднялось на четвёртой, оставшиеся восемь «упали»
            # мгновенно — со стороны это выглядело как «прогон встал посреди
            # четвёртой». Продолжать нечего: пока окно не закрыто, следующий
            # макет обречён так же.
            if _modal_dialog_blocked(exc):
                left = len(targets) - n_done - 1
                note = ("InDesign is blocked by a modal dialog. Close it in "
                        "InDesign and run again; %d document(s) skipped." % left)
                if log.handlers:
                    log.error("%s", note)
                print(note, file=sys.stderr)
                emit("run_blocked", reason="modal_dialog", skipped=left)
                break
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
