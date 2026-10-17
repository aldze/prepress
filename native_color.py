# -*- coding: utf-8 -*-
"""
Памятные цвета без Adobe: маски даёт Apple Vision, всё остальное — ImageMagick.

Полный аналог jsx/color_correct.jsx в части памятных цветов. Правила, ворота
и пороги берутся из тех же разделов config.json и приходят сюда тем же
заданием — расходятся только детекторы и способ положить кривую.

Что чем заменено:

    Select People      -> VNGeneratePersonSegmentationRequest (tools/vision/vmask)
    Select Sky         -> Lab-коробка синевы, прижатая к верху кадра + ярлык сюжета
    «Тона кожи»        -> Lab-коробка, пересечённая с маской людей
    цветовой диапазон  -> Lab-коробка (в Photoshop зелень и так искалась ею)
    кривые Photoshop   -> 256-точечная таблица и -clut
    выделение          -> маска-картинка и трёхслойный -composite

Чего в варианте с Photoshop не было вовсе: **классификатор сюжета**. Vision
на том же проходе отдаёт ярлыки кадра, и «неба» в интерьере или «зелени»
на бильярдном сукне больше не случается — ярлыка sky или grass там просто нет.
Это единственное место, где безадобный вариант не догоняет Photoshop,
а обходит его.

Инварианты, каждый — пойманный баг:

1.  **`-color-threshold` для Lab-коробок не годится.** Он принимает `lab(...)`,
    но разбирает его не в единицах хранимых каналов: кожа с L 43, a 19, b 14
    не попадала в коробку lab(20,5,8)-lab(95,32,48), покрытие лица выходило
    0.7% вместо 71%. Коробка собирается вручную: канал отделяется, и на нём
    двумя `-threshold` строится полоса.

2.  **Каналы Lab у magick хранятся сдвинутыми.** L: 0..100 -> 0..1; a и b:
    -127.5..127.5 -> 0..1, то есть ноль лежит на 50%. Отсюда `_pc()`.

3.  **Площадь маски меряет magick, а не Core Image.** CIAreaAverage усредняет
    в sRGB, и линейная четверть кадра возвращалась как 0.53 (0.25 в степени
    1/2.2). Тот же подвох ждёт любого, кто решит считать среднее фильтрами.

4.  **Среднее по области — это mean(картинка x маска) / mean(маска).**
    Прямого «среднего под маской» у magick нет, а простое среднее по
    замаскированной картинке считает и чёрный фон тоже.

5.  **Одна точка на кривой сдвигает СРЕДНЕЕ области меньше, чем саму точку**
    (концы кривой закреплены). Ровно та же история, что в JSX: правка идёт
    в несколько заходов с перезамером, цель при этом не пересчитывается.

6.  **Кожа меряется по каждому человеку отдельно.** Кадр делится на полосы
    по серединам между лицами: на групповом снимке люди стоят в ряд, и
    вертикальные полосы разводят их без пересечений. Общая маска, поделённая
    «квадратом вокруг лица», у соседей перекрывалась, и пиксель правился дважды.

7.  **Правка кладётся в исходном RGB.** CMYK-исходники этот движок не берёт:
    в нашем порядке прогона цветокоррекция идёт ДО цветоделения, так что
    CMYK на входе — это файл, который таким принесли, и его лучше не трогать.
"""

import json
import math
import os
import subprocess
from pathlib import Path

from messages import msg

# Где лежат помощники и модели. По умолчанию — там, куда их кладёт setup.sh;
# приложение подставляет своё окружением, потому что внутри бандла путь другой
# и меняется при каждом обновлении. Одна переменная на весь набор: разъезжаться
# им незачем, а искать потом, какая из четырёх не подхватилась, — дорого.
TOOLS_DIR = Path(os.environ.get("PREPRESS_TOOLS_DIR")
                 or (Path.home() / "Library/Application Support"
                     "/prepress_automation/tools"))

VISION_TOOL = TOOLS_DIR / "vmask"
SKIN_TOOL = TOOLS_DIR / "vskin"
# Моделей разбора человека может быть НЕСКОЛЬКО, и это не роскошь: они
# сильны в разном. MediaPipe держит групповой кадр (на снимке с двенадцатью
# девушками 11.2% кожи против 4.3% у SCHP), SCHP — крупную фигуру (на
# контровом кадре предплечье одним куском, 4.0% против 2.4% рваными пятнами).
# vskin объединяет их результаты, поэтому каждая добавляет там, где сильна.
# Второй модели может не быть — её ставит tools/install_schp.sh.
SKIN_MODELS = [TOOLS_DIR / "models" / "selfie_multiclass.mlpackage",
               TOOLS_DIR / "models" / "schp_lip.mlpackage"]
SKIN_MODEL = SKIN_MODELS[0]

# Сколько раз перезамерять и дотягивать остаток. Столько же в color_correct.jsx.
CORRECTION_PASSES = 3

INK_INDEX = {"C": 0, "M": 1, "Y": 2, "K": 3}


def available():
    """(готов ли движок, чем объяснить отказ)."""
    if not VISION_TOOL.exists():
        return False, ("the Vision helper is not built (%s) — run ./setup.sh"
                       % VISION_TOOL.name)
    return True, ""


# --------------------------------------------------------------------------- #
#  Обёртки над magick
# --------------------------------------------------------------------------- #

def _run(cmd, stdin=None):
    proc = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:300]
                           or "код %d" % proc.returncode)
    return proc.stdout


def _fx(mg, path, expr):
    return float(_run([mg, str(path), "-format", "%[fx:" + expr + "]", "info:"]).strip())


def _size(mg, path):
    out = _run([mg, str(path) + "[0]", "-format", "%w %h", "info:"]).split()
    return int(out[0]), int(out[1])


def _pc(lab_value):
    """Единица Lab (a или b) -> проценты хранимого канала. См. инвариант 2."""
    return (float(lab_value) + 127.5) / 2.55


def _band(mg, chan, lo_pc, hi_pc, out):
    """Полоса значений одного канала: 1 там, где lo < канал <= hi."""
    _run([mg, str(chan),
          "(", "+clone", "-threshold", "%.4f%%" % lo_pc, ")",
          "(", "-clone", "0", "-threshold", "%.4f%%" % hi_pc, "-negate", ")",
          "-delete", "0", "-compose", "Multiply", "-composite", str(out)])


def _multiply(mg, a, b, out):
    _run([mg, str(a), str(b), "-compose", "Multiply", "-composite", str(out)])


def _rgb_channels(mg, work, workdir, tag):
    """Раскладка на R, G и B — по одному файлу на канал."""
    base = Path(workdir) / ("rgb_%s_%%d.miff" % tag)
    _run([mg, str(work), "-colorspace", "sRGB", "-separate", "+adjoin", str(base)])
    return [Path(str(base).replace("%d", str(i))) for i in range(3)]


def _lab_channels(mg, work, workdir, tag):
    """Раскладка рабочей копии на L, a, b — по одному файлу на канал."""
    base = Path(workdir) / ("lab_%s_%%d.miff" % tag)
    _run([mg, str(work), "-colorspace", "Lab", "-separate", "+adjoin", str(base)])
    return [Path(str(base).replace("%d", str(i))) for i in range(3)]


def _lab_box(mg, chans, lab_min, lab_max, workdir, tag):
    """Маска Lab-коробки: пересечение трёх полос."""
    lo = [float(lab_min[0]), _pc(lab_min[1]), _pc(lab_min[2])]
    hi = [float(lab_max[0]), _pc(lab_max[1]), _pc(lab_max[2])]
    parts = []
    for i in range(3):
        out = Path(workdir) / ("box_%s_%d.png" % (tag, i))
        _band(mg, chans[i], lo[i], hi[i], out)
        parts.append(out)
    mid = Path(workdir) / ("box_%s_ab.png" % tag)
    _multiply(mg, parts[0], parts[1], mid)
    dst = Path(workdir) / ("box_%s.png" % tag)
    _multiply(mg, mid, parts[2], dst)
    return dst


def _area(mg, mask):
    """Доля кадра под маской, в процентах."""
    return _fx(mg, mask, "mean") * 100.0


def _masked_mean_rgb(mg, image, mask, workdir, tag):
    """
    Среднее по области, 0..255 на канал. См. инвариант 4.

    None, если под маской пусто: делить на ноль нечего, да и мерить тоже.
    """
    weight = _fx(mg, mask, "mean")
    if weight <= 1e-6:
        return None
    tmp = Path(workdir) / ("mm_%s.miff" % tag)
    _run([mg, str(image), str(mask), "-compose", "Multiply", "-composite", str(tmp)])
    out = _run([mg, str(tmp), "-format",
                "%[fx:mean.r] %[fx:mean.g] %[fx:mean.b]", "info:"]).split()
    os.unlink(tmp)
    return [min(255.0, float(v) / weight * 255.0) for v in out]


# --------------------------------------------------------------------------- #
#  ICC: цвет области в проценты краски и обратно
# --------------------------------------------------------------------------- #

def _probe(mg, job, color, profiles):
    """Один пиксель через цепочку профилей. Возвращает список каналов 0..255."""
    cmd = [mg, "-size", "1x1", "-depth", "8", "xc:" + color]
    for i, prof in enumerate(profiles):
        if i == 0 and prof.get("colorspace"):
            cmd += ["-colorspace", prof["colorspace"]]
        cmd += ["-intent", job["intent"]]
        if job.get("bpc"):
            cmd += ["-black-point-compensation"]
        cmd += ["-profile", str(prof["icc"])]
    cmd += ["-depth", "8", "txt:-"]
    line = _run(cmd).strip().splitlines()[-1]
    nums = line.split("(", 1)[1].split(")", 1)[0]
    return [float(v) for v in nums.split(",")]


def ink_of_rgb(mg, job, rgb):
    """RGB области -> проценты краски в target_icc."""
    color = "srgb(%d,%d,%d)" % tuple(int(round(max(0, min(255, v)))) for v in rgb)
    vals = _probe(mg, job, color,
                  [{"icc": job["rgbIcc"]}, {"icc": job["targetIcc"]}])
    return [v / 2.55 for v in vals[:4]]


def rgb_of_ink(mg, job, ink):
    """Проценты краски -> RGB в пространстве файла. Обратный ход тем же ICC."""
    color = "cmyk(%.2f%%,%.2f%%,%.2f%%,%.2f%%)" % tuple(
        max(0.0, min(100.0, v)) for v in ink[:4])
    vals = _probe(mg, job, color,
                  [{"icc": job["targetIcc"], "colorspace": "cmyk"},
                   {"icc": job["rgbIcc"]}])
    return vals[:3]


# --------------------------------------------------------------------------- #
#  Кривая и её наложение
# --------------------------------------------------------------------------- #

def _shift_points(in_value, out_value):
    """Три точки: концы закреплены, середина сдвинута. Как shiftCurve в JSX."""
    x = max(1.0, min(254.0, in_value))
    y = max(0.0, min(255.0, out_value))
    return [(0.0, 0.0), (x, y), (255.0, 255.0)]


def _interp(points, x):
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        if x <= x1:
            if x1 <= x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _table(points):
    """Кривая по точкам -> таблица из 256 значений."""
    return [_interp(points, float(i)) for i in range(256)]


def _identity():
    return [float(i) for i in range(256)]


def _compose(first, second):
    """
    Две кривые подряд -> одна. Результат = second(first(x)).

    Правка идёт в несколько заходов (инвариант 5), но класть на полный кадр
    её надо один раз: каждая конверсия — это ещё один круг округлений.
    """
    out = []
    for i in range(256):
        x = max(0.0, min(255.0, first[i]))
        lo = int(x)
        hi = min(255, lo + 1)
        out.append(second[lo] + (second[hi] - second[lo]) * (x - lo))
    return out


def _clut(mg, tables, out):
    """
    Таблица 256x1: в канале R лежит кривая R, в G — G, в B — B.

    Пиксели отдаются magick перечислением через stdin (`txt:-`): собирать
    градиент вызовами -fx или по пикселю было бы и медленнее, и приблизительнее.
    """
    rows = ["# ImageMagick pixel enumeration: 256,1,255,srgb"]
    for i in range(256):
        vals = [int(round(max(0.0, min(255.0, tables[c][i])))) for c in range(3)]
        rows.append("%d,0: (%d,%d,%d)" % (i, vals[0], vals[1], vals[2]))
    _run([mg, "txt:-", "-depth", "8", str(out)], stdin="\n".join(rows) + "\n")


def _apply_all(mg, src, ops, dst, depth, expand=1.0, feather=1.5):
    """
    Все накопленные правки — одной командой magick по полному кадру.

    Каждая пара (таблица, маска) добавляет в цепочку `-clut` под маской:
    после `-composite` в списке снова одна картинка, поэтому правки просто
    ложатся друг за другом. Так полный кадр читается и пишется ровно по разу,
    сколько бы областей на нём ни нашлось — на групповом снимке с шестью
    людьми это разница между одним проходом и семью.

    Маска растягивается до размера кадра прямо здесь: строится она
    по уменьшенной рабочей копии. И край доводится ТОЖЕ здесь, уже
    в пикселях кадра, а не рабочей копии: `_smooth_mask` заполняет дыры
    и снимает россыпь, но его радиус живёт в масштабе уменьшенной копии,
    и после растягивания граница правки снова становится заметной. Сначала
    расширение на `expand` (маска кожи от модели идёт по контуру чуть внутрь,
    и без запаса у края остаётся непоправленная кайма), потом растушёвка
    на `feather` — чтобы правка не обрывалась ступенькой.
    """
    w, h = _size(mg, src)
    cmd = [mg, str(src) + "[0]"]
    for clut, mask in ops:
        edge = []
        if expand > 0:
            # Dilate по полутоновой маске берёт локальный максимум, то есть
            # двигает наружу и уже растушёванный край, а не только границу 0/1.
            edge += ["-morphology", "Dilate", "Disk:%.2f" % expand]
        if feather > 0:
            edge += ["-blur", "0x%.2f" % feather]
        cmd += ["(", "-clone", "0", "-channel", "RGB", str(clut), "-clut", "+channel", ")",
                "(", str(mask), "-resize", "%dx%d!" % (w, h)] + edge + [")",
                "-composite"]
    cmd += ["-depth", str(depth), str(dst)]
    _run(cmd)


# --------------------------------------------------------------------------- #
#  Детекторы
# --------------------------------------------------------------------------- #

def _json_out(text):
    """
    JSON из stdout помощника, даже если рантайм насорил перед ним.

    Core ML пишет свои жалобы в STDOUT, а не в stderr: модель, у которой
    не собирается ANE, печатает «E5RT encountered an STL exception…» прямо
    перед нашей строкой. Строгий json.loads на этом падал, и пайплайн
    считал, что помощник не отработал, — то есть терял модель из-за
    чужого предупреждения. Берём с первой фигурной скобки.
    """
    t = (text or "").strip()
    i = t.find("{")
    if i < 0:
        raise ValueError("no JSON in the helper output: %s" % t[:200])
    return json.loads(t[i:])


def _vision(source, workdir, tag, detect_px):
    """Маска людей, лица и ярлыки сюжета — одним разбором файла."""
    outdir = Path(workdir) / ("vision_%s" % tag)
    outdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(VISION_TOOL), str(source), str(outdir),
                           str(int(detect_px))], capture_output=True, text=True)
    raw = (proc.stdout or "").strip()
    try:
        data = _json_out(raw)
    except ValueError:
        raise RuntimeError("Vision returned something other than JSON: %s"
                           % (raw or (proc.stderr or "").strip())[:200])
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "Vision did not run")
    return data


def installed_skin_models():
    """Какие модели разбора человека есть на этой машине."""
    return [m for m in SKIN_MODELS if m.exists()]


def skin_model_available():
    """Есть ли чем разобрать человека на кожу и одежду."""
    return SKIN_TOOL.exists() and bool(installed_skin_models())


# Насколько модели разошлись, чтобы маске ещё верить. Порог взят из замера
# на 12 кадрах номера: девять нормальных легли в 1.0…1.9, а три, где правка
# выходила полосами (двенадцать девушек, шестьдесят детей, большой групповой
# снимок), дали 2.8, 6.0 и 8.3. Разрыв широкий, порог стоит посередине.
SKIN_DISAGREE_MAX = 2.3

# Во сколько раз разошлись модели на последнем разборе; 0 — согласны.
# Список, а не число, чтобы менять из функции без global.
_LAST_SKIN_DISAGREE = [0.0, 0.0, 0.0]

# Ярлык «люди» на последнем разборе, если vskin сказал «людей нет»; иначе -1.
_LAST_SKIN_NO_PEOPLE = [-1.0]

# Порог ярлыка «люди» у классификатора сюжета; 0 — ворота выключены.
_skin_people_min = 0.3

# Порог на этот прогон: ставится из конфига перед разбором.
_skin_disagree_limit = SKIN_DISAGREE_MAX

# Делитель плитки: 0 — vskin подбирает сам по величине лиц.
_skin_tile_div = 0.0
_skin_tile_step = 0.0

# Почему это вообще работает. Модели ошибаются ПО-РАЗНОМУ: MediaPipe на кадре
# с дюжиной мелких людей заливает «кожей» юбки, грамоты и тёмный фон, а SCHP
# на том же кадре аккуратен, но берёт не всех. Там, где обе видят одно и то же,
# маске можно верить; где одна даёт втрое больше другой — это заливка, и
# правка ляжет полосами: часть голени попадёт в маску, соседняя нет.
# Доля, ниже которой ответ модели считается «кожи нет». Нужна только
# как знаменатель: делить на ноль нельзя.
SKIN_SHARE_FLOOR = 0.0005


def _skin_models_agree(data, limit=SKIN_DISAGREE_MAX):
    """(согласны ли, во сколько раз разошлись). Одна модель — сравнивать не с чем.

    НОЛЬ — ЭТО ОТВЕТ, а не отказ модели. Раньше доли ниже 0.05% выбрасывались
    до сравнения, и на интерьере MediaPipe с честными 0.02% выпал из
    спора: SCHP остался один и его 9.7% «кожи» (оранжевая колонна, потолок)
    прошли без проверки. Отказ модели vskin сообщает иначе — всем ответом,
    а не долей. Пол поднимается только для деления.
    """
    shares = [float(m.get("share") or 0) for m in (data.get("models") or [])]
    if len(shares) < 2 or max(shares) < SKIN_SHARE_FLOOR:
        return True, 1.0
    ratio = max(shares) / max(min(shares), SKIN_SHARE_FLOOR)
    return ratio <= limit, ratio


def _skin_model_mask(mg, source, size, workdir, tag):
    """
    Маска КОЖИ от модели разбора человека, приведённая к рабочему размеру.

    Зачем она вместо коробки по цвету. Отделить кожу от ткани телесного
    оттенка цветом нельзя в принципе: на кадре с вручением белые шорты
    женщины и хаки соседа лежат ровно в той же коробке Lab, что и её ноги,
    и никакой порог по красноте этого не лечит — он либо пропускает ткань,
    либо отрезает бледное лицо. Модель размечает кожу отдельно от одежды
    ПО СМЫСЛУ: на том же кадре брюки остались снаружи, а голени внутри.

    None — модели нет или она не отработала; тогда вызывающая сторона
    возвращается к коробке по цвету. ПУСТАЯ маска — это не None: «кожи
    в кадре нет» есть ответ модели, и откатываться после него на коробку
    нельзя (см. ниже).
    """
    if not skin_model_available():
        return None
    outdir = Path(workdir) / ("vskin_%s" % tag)
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [str(SKIN_TOOL), "--people-min=%g" % _skin_people_min]
    if _skin_tile_div > 0:
        cmd.append("--tile-div=%g" % _skin_tile_div)
    if _skin_tile_step > 0:
        cmd.append("--tile-step=%g" % _skin_tile_step)
    cmd += [str(source), str(outdir)] + [str(m) for m in installed_skin_models()]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = _json_out(proc.stdout)
    except ValueError:
        return None
    if not data.get("ok"):
        return None
    if data.get("people") is False:
        # Людей в кадре нет — ни лиц, ни фигур, ни ярлыка сюжета. Это ответ,
        # а не отказ: маска пустая, моделей vskin даже не грузил.
        _LAST_SKIN_NO_PEOPLE[0] = float(data.get("peopleLabel") or 0)
        dst = Path(workdir) / ("skinm_%s.png" % tag)
        _run([mg, "-size", "%dx%d" % size, "xc:black", "-colorspace", "Gray", str(dst)])
        return dst
    ok, ratio = _skin_models_agree(data, _skin_disagree_limit)
    if not ok:
        # НЕ откат на коробку по цвету: она на таком кадре течёт ещё сильнее.
        # Отказ означает «эту картинку не правим», и решает это вызывающая
        # сторона — ей и сообщаем числом.
        got = sorted((float(m.get("share") or 0) for m in (data.get("models") or [])),
                     reverse=True)
        _LAST_SKIN_DISAGREE[:] = [ratio, (got[0] if got else 0) * 100,
                                  (got[-1] if got else 0) * 100]
        return None
    _LAST_SKIN_DISAGREE[:] = [0.0, 0.0, 0.0]
    raw = Path(data.get("skin") or "")
    if not raw.exists():
        return None
    # Пустую маску возвращаем как есть, а не как отказ: «кожи в кадре нет» —
    # это ОТВЕТ модели, и откатываться после него на коробку по цвету нельзя.
    # Коробка на пейзаже уверенно находит «кожу» в деревянной стене, ради чего
    # её и приходится пересекать с маской людей.
    dst = Path(workdir) / ("skinm_%s.png" % tag)
    _run([mg, str(raw), "-resize", "%dx%d!" % size, "-threshold", "50%", str(dst)])
    return dst


def _scene_ok(labels, wanted, minimum):
    """Ворота по сюжету: хотя бы один ярлык из списка не ниже порога."""
    if not wanted:
        return True, ""
    best_name, best = "", 0.0
    for name in wanted:
        v = float(labels.get(name, 0.0))
        if v > best:
            best_name, best = name, v
    if best >= minimum:
        return True, "%s %.2f" % (best_name, best)
    return False, msg("mem_scene_no", best=("%s %.2f" % (best_name, best))
                      if best_name else msg("scene_none"))


def _person_mask(mg, vision, size, workdir, tag):
    """Маска людей, приведённая к размеру рабочей копии и загрублённая до 0/1."""
    path = vision.get("person")
    if not path or not Path(path).exists():
        return None
    dst = Path(workdir) / ("person_%s.png" % tag)
    _run([mg, str(path), "-resize", "%dx%d!" % size, "-threshold", "50%", str(dst)])
    return dst


def _person_bands(vision, size, workdir, tag, mg):
    """
    Вертикальные полосы по серединам между лицами. См. инвариант 6.

    Возвращает [(подпись, маска)]. Лиц нет — пустой список, и кожа меряется
    по всем людям сразу.
    """
    faces = sorted(vision.get("faces") or [], key=lambda f: f["x"] + f["w"] / 2.0)
    if len(faces) < 2:
        return []
    w, h = size
    centers = [f["x"] + f["w"] / 2.0 for f in faces]
    bounds = [0.0]
    for i in range(1, len(centers)):
        bounds.append((centers[i - 1] + centers[i]) / 2.0)
    bounds.append(1.0)
    out = []
    for i in range(len(faces)):
        x0 = int(round(bounds[i] * w))
        x1 = int(round(bounds[i + 1] * w))
        if x1 - x0 < 4:
            continue
        dst = Path(workdir) / ("band_%s_%d.png" % (tag, i))
        _run([mg, "-size", "%dx%d" % (w, h), "xc:black",
              "-fill", "white", "-draw", "rectangle %d,0 %d,%d" % (x0, x1 - 1, h - 1),
              "-colorspace", "Gray", str(dst)])
        out.append((msg("person_n", n=i + 1), dst))
    return out


def _smooth_mask(mg, mask, radius, out):
    """
    Сгладить маску: заполнить дыры, убрать россыпь, растушевать край.

    Без этого правка ложится сквозь ДЫРЯВУЮ маску: соседние пиксели кожи один
    поправлен, другой нет, и на теле выступают пятна — на светлом кадре они
    читаются как фиолетовые кляксы. Photoshop такой беды не знает: его цветовой
    диапазон отдаёт мягкое выделение с растушёвкой, а голая пороговая коробка —
    жёсткое «да/нет» на каждый пиксель.

    Close заполняет дыры внутри области, Open убирает одиночные точки снаружи,
    blur делает край мягким, чтобы правка не обрывалась ступенькой.
    """
    r = max(1, int(radius))
    _run([mg, str(mask),
          "-morphology", "Close", "Disk:%d" % r,
          "-morphology", "Open", "Disk:%d" % max(1, r - 1),
          "-blur", "0x%d" % r, str(out)])
    return out


def _face_reference(mg, work, face, size, workdir, tag):
    """
    Средний Lab в середине прямоугольника лица — эталон кожи ЭТОГО человека.

    Берётся центральная половина прямоугольника: по краям туда попадают волосы,
    фон и тень от козырька, а в середине почти наверняка кожа.

    Зачем вообще эталон. Фиксированная коробка Lab исключала ровно ту кожу,
    которую надо лечить: на холодном кадре у лиц b выходил 6.2 и 1.0 при нижней
    границе коробки 8. Круг замыкался — чем сильнее увод, тем меньше маска его
    ловит, и правились случайные пиксели, то есть пятнами.
    """
    w, h = size
    cw = max(2, int(face["w"] * w * 0.5))
    ch = max(2, int(face["h"] * h * 0.5))
    cx = int((face["x"] + face["w"] / 2.0) * w - cw / 2.0)
    cy = int((face["y"] + face["h"] / 2.0) * h - ch / 2.0)
    cx = max(0, min(w - cw, cx))
    cy = max(0, min(h - ch, cy))
    out = _run([mg, str(work), "-crop", "%dx%d+%d+%d" % (cw, ch, cx, cy), "+repage",
                "-colorspace", "Lab", "-format",
                "%[fx:mean.r*100] %[fx:mean.g*255-127.5] %[fx:mean.b*255-127.5]",
                "info:"]).split()
    return [float(v) for v in out]


def _pixel(mg, mask, x, y):
    """Значение маски в точке, 0..1."""
    try:
        return float(_run([mg, str(mask), "-format",
                           "%%[fx:p{%d,%d}]" % (int(x), int(y)), "info:"]).strip())
    except Exception:
        return 0.0


def _subtract(mg, base, other, out):
    """base без other: маска фона минус передний план."""
    _run([mg, str(base), "(", str(other), "-negate", ")",
          "-compose", "Multiply", "-composite", str(out)])
    return out


def _instance_masks(mg, vision, size, workdir, tag):
    """Маски отдельных людей от Vision, приведённые к размеру рабочей копии."""
    out = []
    for i, path in enumerate(vision.get("persons") or []):
        if not path or not Path(path).exists():
            continue
        dst = Path(workdir) / ("inst_%s_%d.png" % (tag, i))
        _run([mg, str(path), "-resize", "%dx%d!" % size, "-threshold", "50%", str(dst)])
        out.append(dst)
    return out


def _split_by_faces(mg, mask, faces, size, workdir, tag):
    """
    Поделить одну маску вертикальными полосами по серединам между лицами.

    Нужно там, где Vision слепил несколько человек в один экземпляр: на плотной
    группе из шести он отдаёт три. Полосы — геометрия грубая, но внутри УЖЕ
    отделённой от фона маски человека она работает: люди в группе стоят в ряд.
    """
    if len(faces) < 2:
        return [(mask, faces[0] if faces else None)]
    w, h = size
    centers = sorted(f["x"] + f["w"] / 2.0 for f in faces)
    order = sorted(faces, key=lambda f: f["x"] + f["w"] / 2.0)
    bounds = [0.0] + [(centers[i - 1] + centers[i]) / 2.0
                      for i in range(1, len(centers))] + [1.0]
    out = []
    for i, face in enumerate(order):
        x0 = int(round(bounds[i] * w))
        x1 = int(round(bounds[i + 1] * w))
        if x1 - x0 < 4:
            continue
        band = Path(workdir) / ("sp_%s_%d.png" % (tag, i))
        _run([mg, "-size", "%dx%d" % (w, h), "xc:black", "-fill", "white",
              "-draw", "rectangle %d,0 %d,%d" % (x0, x1 - 1, h - 1),
              "-colorspace", "Gray", str(band)])
        part = Path(workdir) / ("spm_%s_%d.png" % (tag, i))
        _multiply(mg, mask, band, part)
        out.append((part, face))
    return out


def _people_regions(mg, vision, size, workdir, tag):
    """
    Пары «маска человека, его лицо» — основа для замера кожи по каждому.

    Порядок такой. Vision отдаёт маски отдельных людей; каждое лицо
    приписывается к той маске, внутри которой лежит его центр. Дальше:
    один человек на маске — берём как есть; несколько лиц на одной маске
    (Vision слепил) — делим её полосами; лиц на маске нет вовсе — пропускаем,
    потому что без лица неоткуда взять эталон цвета кожи.

    Экземпляров нет совсем — возвращаем пусто, и вызывающая сторона работает
    по общей маске людей, как раньше.
    """
    faces = vision.get("faces") or []
    insts = _instance_masks(mg, vision, size, workdir, tag)
    if not insts or not faces:
        return []
    w, h = size
    by_inst = dict((i, []) for i in range(len(insts)))
    homeless = []
    for face in faces:
        cx = (face["x"] + face["w"] / 2.0) * w
        cy = (face["y"] + face["h"] / 2.0) * h
        hit = None
        for i, m in enumerate(insts):
            if _pixel(mg, m, min(w - 1, cx), min(h - 1, cy)) > 0.5:
                hit = i
                break
        (by_inst[hit] if hit is not None else homeless).append(face)
    regions = []
    for i, m in enumerate(insts):
        got = by_inst[i]
        if not got:
            continue
        regions.extend(_split_by_faces(mg, m, got, size, workdir,
                                       "%s_%d" % (tag, i)))
    # Лицо, не попавшее ни в один экземпляр (человек за кадром, отражение,
    # промах модели), остаётся без маски — молча его терять нельзя.
    return regions, homeless


def _row_profile(mg, mask, height):
    """Доля закрашенного в каждой строке маски, сверху вниз."""
    txt = _run([mg, str(mask), "-resize", "1x%d!" % int(height), "-depth", "8", "txt:-"])
    rows = []
    for line in txt.splitlines()[1:]:
        try:
            rows.append(float(line.split("(", 1)[1].split(",", 1)[0]) / 255.0)
        except Exception:
            rows.append(0.0)
    return rows


def _sky_horizon(mg, box, size, opts, workdir, tag):
    """
    Где кончается небо — по самой маске, а не по доле кадра.

    Почему не `VNDetectHorizonRequest`: он отдаёт УГОЛ НАКЛОНА камеры,
    а не высоту линии, и находится далеко не всегда — на трёх проверенных
    кадрах клуба сработал на одном. Положение линии из его transform
    не достаётся.

    Зато строчный профиль самой маски синевы отвечает на вопрос прямо: идём
    сверху вниз, пока строки в основном синие, и останавливаемся там, где
    синева кончилась и больше не возвращается. Ниже этой черты «синее» —
    это вода, бассейн или чья-то рубашка, и правила неба их не касаются.

    Не нашлось внятной границы — возвращаем долю из настроек, как раньше.
    """
    w, h = size
    fallback = max(1, int(round(h * float(opts.get("sky_top_share", 0.62)))))
    rows = _row_profile(mg, box, h)
    if not rows:
        return fallback
    floor = float(opts.get("sky_row_min", 0.15))
    # Последняя строка сверху, ниже которой синевы уже устойчиво нет.
    edge = 0
    for i, v in enumerate(rows):
        if v >= floor:
            edge = i
        elif i - edge > h * 0.08:      # провал шире 8% высоты — это конец неба
            break
    if edge < h * 0.05:                # неба по сути нет
        return fallback
    return min(h, edge + max(2, int(h * 0.02)))


def _sky_mask(mg, chans, size, opts, workdir, tag):
    """
    Небо: Lab-коробка синевы, прижатая к верху кадра.

    Прижатие обязательно: та же синева внизу кадра — это вода, а её правила
    неба не касаются. Порог по высоте мягкий, полосой: резкая граница
    оставляла бы на снимке видимый шов ровно там, где кончается правка.
    Сама черта считается по маске (см. _sky_horizon), а доля из настроек
    остаётся запасным вариантом.
    """
    box = _lab_box(mg, chans, opts.get("sky_lab_min") or [35, -40, -75],
                   opts.get("sky_lab_max") or [100, 12, -3], workdir, tag + "_sky")
    w, h = size
    edge = (_sky_horizon(mg, box, size, opts, workdir, tag)
            if opts.get("vision_extras", False)
            else max(1, int(round(size[1] * float(opts.get("sky_top_share", 0.62))))))
    ramp = max(2, int(round(h * 0.08)))
    grad = Path(workdir) / ("skytop_%s.png" % tag)
    # Белое сверху, мягкий спуск в чёрное к высоте edge.
    _run([mg, "-size", "%dx%d" % (w, max(1, edge - ramp)), "xc:white",
          "(", "-size", "%dx%d" % (w, ramp), "gradient:white-black", ")",
          "(", "-size", "%dx%d" % (w, max(1, h - edge)), "xc:black", ")",
          "-append", "-resize", "%dx%d!" % (w, h), "-colorspace", "Gray", str(grad)])
    dst = Path(workdir) / ("sky_%s.png" % tag)
    _multiply(mg, box, grad, dst)
    return dst


# --------------------------------------------------------------------------- #
#  Арифметика правил — та же, что в color_correct.jsx
# --------------------------------------------------------------------------- #

def _rule_value(ink, rule):
    v = ink[INK_INDEX[rule["ch"]]]
    if rule.get("kind") == "ratio":
        ref = ink[INK_INDEX[rule["ref"]]]
        if ref <= 0.5:
            return None            # делить на почти-ноль бессмысленно
        return v / ref
    if rule.get("kind") == "diff":
        return v - ink[INK_INDEX[rule["ref"]]]
    return v


def _ink_for_bound(ink, rule, bound):
    if rule.get("kind") == "ratio":
        return ink[INK_INDEX[rule["ref"]]] * bound
    if rule.get("kind") == "diff":
        return ink[INK_INDEX[rule["ref"]]] + bound
    return bound


def _gates_failed(ink, gates):
    for g in (gates or []):
        v = _rule_value(ink, g)
        if v is None:
            return g
        if g.get("min") is not None and v < g["min"]:
            return g
        if g.get("max") is not None and v > g["max"]:
            return g
    return None


def _plan(ink, rules, max_shift, strength):
    plan = []
    for r in (rules or []):
        v = _rule_value(ink, r)
        if v is None:
            continue
        bound = None
        # Прицел: ведём к середине диапазона ВСЕГДА, а не только при выходе
        # за границу. См. _rule() в prepress.py — там числа, ради которых
        # это появилось.
        if r.get("aim") is not None:
            bound = r["aim"]
        elif r.get("min") is not None and v < r["min"]:
            bound = r["min"]
        elif r.get("max") is not None and v > r["max"]:
            bound = r["max"]
        if bound is None:
            continue
        idx = INK_INDEX[r["ch"]]
        want = max(0.0, min(100.0, _ink_for_bound(ink, r, bound)))
        delta = (want - ink[idx]) * strength
        if abs(delta) > max_shift:
            delta = (1 if delta > 0 else -1) * max_shift
        if abs(delta) < 0.5:        # ниже полупроцента печать не покажет
            continue
        plan.append({"ch": r["ch"], "from": ink[idx], "to": ink[idx] + delta,
                     "was": v, "bound": bound, "rule": r})
    return plan


def _ink_string(ink):
    return "/".join(str(int(round(v))) for v in ink[:4])


def _shift_string(plan):
    """Строка правок для отчёта. Формат общий с color_correct.jsx."""
    return "|".join("%s:%.1f:%.1f:%.4f:%.4f:%s:%s"
                    % (p["ch"], p["from"], p["to"], p["was"], p["bound"],
                       "1" if p["rule"].get("aim") is not None else "0",
                       p["rule"].get("label", ""))
                    for p in plan)


# --------------------------------------------------------------------------- #
#  Один файл
# --------------------------------------------------------------------------- #

# Ярлыки Vision, подтверждающие сюжет. Классификатор отдаёт иерархию
# (grass входит в land, blue_sky в sky), поэтому списки короткие.
SCENE_GATES = {
    "sky": ["sky", "blue_sky", "cloudy", "outdoor", "sunset_sunrise"],
    "grass": ["grass", "golf_course", "lawn", "plant", "tree", "outdoor"],
}


def _region_find(mg, job, spec, mask, image, workdir, tag, part):
    """
    Замер одной области и решение по ней.

    Возвращает (находка, план, среднее RGB). План пустой — правки не будет,
    причина уже записана в находку.
    """
    name = spec["name"]
    area = _area(mg, mask)
    base = {"color": name, "part": part, "area": round(area, 1),
            "before": "", "after": "", "shift": "", "why": ""}
    if area < float(spec.get("minArea", 0.5)):
        base["status"] = "small"
        base["why"] = msg("mem_small", area=area, min=float(spec.get("minArea", 0.5)))
        return base, [], None

    rgb = _masked_mean_rgb(mg, image, mask, workdir, tag)
    if rgb is None:
        base["status"] = "none"
        return base, [], None
    ink = ink_of_rgb(mg, job, rgb)
    base["before"] = _ink_string(ink)

    failed = _gates_failed(ink, spec.get("gates"))
    if failed is not None:
        base["status"] = "unlike"
        base["why"] = failed.get("label") or ""
        return base, [], rgb

    plan = _plan(ink, spec.get("rules"), float(job.get("maxShift", 12)),
                 float(job.get("strength", 0.7)))
    if not plan:
        base["status"] = "ok"
        return base, [], rgb
    base["status"] = "corrected"
    base["shift"] = _shift_string(plan)
    return base, plan, rgb


def _region_correct(mg, job, work, mask, plan, ink, workdir, tag):
    """
    Кривые для одной области: несколько заходов с перезамером.

    Считается всё на уменьшенной рабочей копии — она же и правится, чтобы
    следующая область мерила уже исправленный кадр. Полный кадр правится
    потом один раз накопленной таблицей (см. _apply_all).

    Возвращает (таблицы по каналам или None, краски после правки).
    """
    want = list(ink)
    for p in plan:
        want[INK_INDEX[p["ch"]]] = p["to"]
    channels = [p["ch"] for p in plan]

    composed = [_identity(), _identity(), _identity()]
    touched = False
    after = list(ink)
    for _ in range(CORRECTION_PASSES):
        cur = list(after)
        goal = list(cur)
        left = 0.0
        for ch in channels:
            idx = INK_INDEX[ch]
            goal[idx] = want[idx]
            left = max(left, abs(goal[idx] - cur[idx]))
        if left < 0.7:             # ближе печать всё равно не покажет
            break

        # И вход, и выход считаются через один круг ICC, чтобы неточность
        # обратного перевода вычиталась сама.
        rgb_now = rgb_of_ink(mg, job, cur)
        rgb_want = rgb_of_ink(mg, job, goal)
        means = _masked_mean_rgb(mg, work, mask, workdir, tag)
        if means is None:
            break

        tables = []
        moved = False
        for c in range(3):
            delta = rgb_want[c] - rgb_now[c]
            if abs(delta) < 0.4:
                tables.append(_identity())
                continue
            tables.append(_table(_shift_points(means[c], means[c] + delta)))
            moved = True
        if not moved:
            break

        clut = Path(workdir) / ("clut_%s.png" % tag)
        _clut(mg, tables, clut)
        # Доводка меряется на той же маске, какой правка ляжет на кадр:
        # иначе замер «стало» считается по резкому краю, а в файл уходит
        # растушёванный, и числа в отчёте расходятся с оттиском.
        _edge = job.get("native") or {}
        _apply_all(mg, work, [(clut, mask)], work, 16,
                   expand=float(_edge.get("mask_expand", 1.0)),
                   feather=float(_edge.get("mask_feather", 1.5)))
        composed = [_compose(composed[c], tables[c]) for c in range(3)]
        touched = True

        # Что вышло на самом деле — замеряем, а не считаем: кривая работает
        # по всей области, а не по её среднему.
        got = _masked_mean_rgb(mg, work, mask, workdir, tag)
        if got is None:
            break
        after = ink_of_rgb(mg, job, got)

    return (composed if touched else None), after


def last_skin_disagree():
    """(во сколько раз разошлись, больше %, меньше %) на последнем разборе.

    Нужно вызывающей стороне: отказ обязан попасть в ОТЧЁТ предупреждением,
    а не только строкой журнала — среди сотни строк его иначе не заметить.
    """
    return tuple(_LAST_SKIN_DISAGREE)


def _set_skin_limits(opts):
    """Порог доверия к маске на этот кадр и сброс прошлого замера."""
    global _skin_disagree_limit, _skin_tile_div, _skin_tile_step, _skin_people_min
    _skin_disagree_limit = float((opts or {}).get("skin_disagree_max")
                                 or SKIN_DISAGREE_MAX)
    _skin_people_min = float((opts or {}).get("skin_people_min", 0.3))
    _LAST_SKIN_NO_PEOPLE[0] = -1.0
    _skin_tile_div = float((opts or {}).get("skin_tile_div") or 0)
    _skin_tile_step = float((opts or {}).get("skin_tile_step") or 0)
    _LAST_SKIN_DISAGREE[:] = [0.0, 0.0, 0.0]


def correct(job):
    """
    Памятные цвета одного файла без Adobe.

    Задание и ответ — той же формы, что у color_correct.jsx: вызывающая
    сторона не должна знать, какой движок отработал.
    """
    mg = job["magick"]
    src = Path(job["src"])
    dst = Path(job["dst"])
    workdir = Path(job["workdir"])
    tag = str(job["tag"])
    opts = job.get("native") or {}
    _set_skin_limits(opts)
    findings = []
    warnings = []

    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    if not src.exists():
        raise RuntimeError("file not found on disk")

    # Правка кладётся в исходном RGB — см. инвариант 7.
    cs = str(job.get("colorspace") or "").lower()
    if "rgb" not in cs:
        return {"ok": True, "changed": False, "findings": findings,
                "warnings": warnings,
                "skipped": msg("mem_cmyk", cs=(cs or "?").upper())}

    # Рабочая копия: только уменьшение, без конверсий. Числа меряются
    # в значениях самого файла, а в проценты краски их переводит ICC.
    detect_px = int(job.get("detectPx", 2000))
    work = workdir / ("nwork_%s.miff" % tag)
    _run([mg, str(src) + "[0]", "-resize", "%dx%d>" % (detect_px, detect_px),
          "-alpha", "off", "-depth", "16", str(work)])
    size = _size(mg, work)

    vision = _vision(src, workdir, tag, detect_px)
    labels = vision.get("labels") or {}
    chans = _lab_channels(mg, work, workdir, tag)
    person = _person_mask(mg, vision, size, workdir, tag)
    subject = None
    sub_path = vision.get("subject")
    if sub_path and Path(sub_path).exists():
        subject = workdir / ("subject_%s.png" % tag)
        _run([mg, str(sub_path), "-resize", "%dx%d!" % size,
              "-threshold", "50%", str(subject)])
    scene_min = float(opts.get("scene_min_confidence", 0.3))
    # Улучшения Vision, появившиеся ПОСЛЕ принятого прогона: маски отдельных
    # людей вместо вертикальных полос, вычитание переднего плана из неба
    # и зелени, горизонт по профилю маски. Каждое проверено на нескольких
    # кадрах, но НЕ на полном номере, а сегодняшний день показал, чего стоит
    # непроверенное. Выключатель нужен, чтобы конфигурация принятого прогона
    # оставалась воспроизводимой.
    extras = bool(opts.get("vision_extras", False))

    ops = []
    for spec in (job.get("colors") or []):
        name = spec["name"]
        regions = []
        try:
            if name == "skin":
                # Кожу размечает МОДЕЛЬ разбора человека, а цветовая коробка
                # остаётся запасным путём. Порядок именно такой: цвет отделить
                # кожу от ткани телесного оттенка не может в принципе,
                # и все пойманные за день сбои (серая майка, телесные брюки,
                # белые шорты) — это протечки коробки, а не ошибки правки.
                model = (_skin_model_mask(mg, src, size, workdir, tag)
                         if opts.get("skin_model", True) else None)
                # Модели разошлись — кожу на этом кадре НЕ правим вовсе.
                # Откат на коробку по цвету тут был бы хуже отказа: она течёт
                # на тех же кадрах и сильнее. Полголени, покрашенной тёплым,
                # рядом с непокрашенной — брак заметнее, чем нетронутый кадр.
                if model is None and _LAST_SKIN_DISAGREE[0] > 0:
                    findings.append({"color": name, "part": "", "status": "skipped",
                                     "area": 0, "before": "", "after": "", "shift": "",
                                     "why": "the two human-parsing models disagree "
                                            "%.1ffold — the mask is not trustworthy"
                                            % _LAST_SKIN_DISAGREE[0]})
                    continue
                if _LAST_SKIN_NO_PEOPLE[0] >= 0:
                    findings.append({"color": name, "part": "", "status": "none",
                                     "area": 0, "before": "", "after": "", "shift": "",
                                     "why": msg("mem_no_people",
                                                label=_LAST_SKIN_NO_PEOPLE[0])})
                    continue
                if person is None and model is None:
                    findings.append({"color": name, "part": "", "status": "none",
                                     "area": 0, "before": "", "after": "",
                                     "shift": "", "why": ""})
                    continue
                smooth = int(opts.get("mask_smooth", 2))
                tol = opts.get("skin_tolerance") or [45, 12, 18]
                # Абсолютный порог по КРАСНОТЕ. Допуск вокруг цвета лица —
                # относительный, и на лице с a=13 он опускает нижнюю границу
                # до a=1, куда свободно проходит белая и бежевая ткань
                # (у неё a около нуля). На реальном кадре так в «кожу» попали
                # рубашка и поло: четверть замеряемого куска майки лежала
                # внутри маски, и правка кожи (голубая с 34 до 22) уводила
                # светлые складки в тепло. С порогом утечка ровно ноль,
                # а маска кожи худеет с 8.8% кадра до 4.8% — уходит лишнее.
                a_min = float(opts.get("skin_a_min", 3))
                faces = sorted(vision.get("faces") or [],
                               key=lambda f: f["x"] + f["w"] / 2.0)
                bands = (_person_bands(vision, size, workdir, tag, mg)
                         if spec.get("perPerson") else [])
                # Маски отдельных людей от Vision — настоящая геометрия
                # человека вместо вертикальной полосы. Где Vision слепил
                # двоих в одного, маска делится полосой уже ВНУТРИ человека,
                # а не по всему кадру.
                people = []
                if spec.get("perPerson") and extras:
                    got = _people_regions(mg, vision, size, workdir, tag)
                    if got:
                        people = got[0]
                if model is not None:
                    # Порог по площади калибровался под ТЕКУЧУЮ коробку,
                    # где к коже добавлялась половина светлой одежды. Точная
                    # маска втрое меньше: на кадре с корветом вся кожа — это
                    # 0.45% кадра на двоих, и при пороге 0.5% правка не
                    # делалась бы вовсе именно там, где она нужнее всего
                    # (лица в тени). Порог опущен до 0.1% — на рабочей копии
                    # 854 px это около 500 пикселей, то есть лицо 22x22;
                    # меньше уже шум, а не замер.
                    spec = dict(spec)
                    spec["minArea"] = float(opts.get("skin_model_min_area", 0.1))
                    # Маска уже знает, где кожа. Осталось разложить её
                    # по людям — ради раздельного замера и правки: на одном
                    # кадре бывает и загорелый на солнце, и бледный в тени,
                    # и общая кривая портит обоих.
                    if people:
                        for i, (pmask, _face) in enumerate(people):
                            raw = workdir / ("skinr_%s_%d.png" % (tag, i))
                            _multiply(mg, model, pmask, raw)
                            part_mask = workdir / ("skinp_%s_%d.png" % (tag, i))
                            _smooth_mask(mg, raw, smooth, part_mask)
                            regions.append((msg("person_n", n=i + 1), part_mask))
                    elif bands:
                        for i, (label, band) in enumerate(bands):
                            raw = workdir / ("skinb_%s_%d.png" % (tag, i))
                            _multiply(mg, model, band, raw)
                            part_mask = workdir / ("skinp_%s_%d.png" % (tag, i))
                            _smooth_mask(mg, raw, smooth, part_mask)
                            regions.append((label, part_mask))
                    else:
                        # Делить некого — правим всю найденную кожу разом.
                        whole = workdir / ("skin_%s.png" % tag)
                        _smooth_mask(mg, model, smooth, whole)
                        regions.append(("", whole))
                    # Модель молчит, а лица в кадре есть — это не норма,
                    # и знать об этом надо из отчёта, а не по недоправленному
                    # кадру в оттиске.
                    if faces and not _area(mg, model):
                        warnings.append(msg("mem_no_skin_model", faces=len(faces)))
                elif people:
                    for i, (pmask, face) in enumerate(people):
                        if face is None:
                            continue
                        ref = _face_reference(mg, work, face, size, workdir, tag)
                        # Порог не должен отсекать ту кожу, по которой снят
                        # эталон: на кадре с корветом у второго лица эталон
                        # был a=5 при пороге 6, и его собственное лицо
                        # из маски выпадало. Отсюда потолок по эталону.
                        lo = [ref[0] - tol[0],
                              max(ref[1] - tol[1], min(a_min, ref[1] - 2.0)),
                              ref[2] - tol[2]]
                        hi = [ref[0] + tol[0], ref[1] + tol[1], ref[2] + tol[2]]
                        box = _lab_box(mg, chans, lo, hi, workdir,
                                       "%s_skin%d" % (tag, i))
                        raw = workdir / ("skinr_%s_%d.png" % (tag, i))
                        _multiply(mg, box, pmask, raw)
                        part_mask = workdir / ("skinp_%s_%d.png" % (tag, i))
                        _smooth_mask(mg, raw, smooth, part_mask)
                        regions.append((msg("person_n", n=i + 1), part_mask))
                elif faces:
                    # Экземпляров нет — работаем по общей маске людей, деля
                    # её полосами. Коробка всё равно строится вокруг
                    # ЗАМЕРЕННОГО цвета лица: так она подходит и бледной коже
                    # на холодном свету, и загорелой на закате.
                    for i, face in enumerate(faces):
                        ref = _face_reference(mg, work, face, size, workdir, tag)
                        # Порог не должен отсекать ту кожу, по которой снят
                        # эталон: на кадре с корветом у второго лица эталон
                        # был a=5 при пороге 6, и его собственное лицо
                        # из маски выпадало. Отсюда потолок по эталону.
                        lo = [ref[0] - tol[0],
                              max(ref[1] - tol[1], min(a_min, ref[1] - 2.0)),
                              ref[2] - tol[2]]
                        hi = [ref[0] + tol[0], ref[1] + tol[1], ref[2] + tol[2]]
                        box = _lab_box(mg, chans, lo, hi, workdir,
                                       "%s_skin%d" % (tag, i))
                        raw = workdir / ("skinr_%s_%d.png" % (tag, i))
                        _multiply(mg, box, person, raw)
                        if i < len(bands):
                            banded = workdir / ("skinb_%s_%d.png" % (tag, i))
                            _multiply(mg, raw, bands[i][1], banded)
                            raw = banded
                        part_mask = workdir / ("skinp_%s_%d.png" % (tag, i))
                        _smooth_mask(mg, raw, smooth, part_mask)
                        label = bands[i][0] if i < len(bands) else ""
                        regions.append((label, part_mask))
                else:
                    # Лиц не нашлось — остаётся коробка по общим числам.
                    # Она грубее, поэтому и сглаживание тут важнее всего.
                    box = _lab_box(mg, chans, opts.get("skin_lab_min") or [20, 5, 8],
                                   opts.get("skin_lab_max") or [95, 32, 48],
                                   workdir, tag + "_skin")
                    raw = workdir / ("skinr_%s.png" % tag)
                    _multiply(mg, box, person, raw)
                    whole = workdir / ("skin_%s.png" % tag)
                    _smooth_mask(mg, raw, smooth, whole)
                    regions.append(("", whole))
            elif name == "sky":
                passed, note = _scene_ok(labels, SCENE_GATES["sky"], scene_min)
                if not passed:
                    findings.append({"color": name, "part": "", "status": "unlike",
                                     "area": 0, "before": "", "after": "",
                                     "shift": "", "why": note})
                    continue
                sky_raw = _sky_mask(mg, chans, size, opts, workdir, tag)
                if subject is not None and extras:
                    # Небо — это фон. Голубая рубашка и синие джинсы стоят
                    # на переднем плане, и правила неба их не касаются.
                    sky_raw = _subtract(mg, sky_raw, subject,
                                        workdir / ("skyf_%s.png" % tag))
                sky = workdir / ("skys_%s.png" % tag)
                regions.append(("", _smooth_mask(mg, sky_raw,
                                                 int(opts.get("mask_smooth", 2)), sky)))
            else:
                passed, note = _scene_ok(labels, SCENE_GATES.get(name) or [], scene_min)
                if not passed:
                    findings.append({"color": name, "part": "", "status": "unlike",
                                     "area": 0, "before": "", "after": "",
                                     "shift": "", "why": note})
                    continue
                raw = _lab_box(mg, chans, spec.get("labMin") or [20, -45, 5],
                               spec.get("labMax") or [80, -8, 60],
                               workdir, tag + "_" + name)
                if subject is not None and extras:
                    # Зелёная рубашка на человеке — не трава.
                    raw = _subtract(mg, raw, subject,
                                    workdir / ("%s_%s_bg.png" % (name, tag)))
                soft = workdir / ("%s_%s_soft.png" % (name, tag))
                regions.append(("", _smooth_mask(mg, raw,
                                                 int(opts.get("mask_smooth", 2)), soft)))
        except Exception as exc:
            findings.append({"color": name, "part": "", "status": "error",
                             "area": 0, "before": "", "after": "", "shift": "",
                             "why": str(exc)[:200]})
            continue

        for part, mask in regions:
            try:
                found, plan, rgb = _region_find(mg, job, spec, mask, work,
                                                workdir, tag, part)
            except Exception as exc:
                findings.append({"color": name, "part": part, "status": "error",
                                 "area": 0, "before": "", "after": "", "shift": "",
                                 "why": str(exc)[:200]})
                continue
            if not plan:
                findings.append(found)
                continue
            if not job.get("apply", True):
                found["status"] = "would"
                found["after"] = found["before"]
                findings.append(found)
                continue
            ink = ink_of_rgb(mg, job, rgb)
            tables, after = _region_correct(mg, job, work, mask, plan, ink,
                                            workdir, tag + "_" + name + str(len(ops)))
            found["after"] = _ink_string(after)
            findings.append(found)
            if tables is not None:
                clut = workdir / ("clutf_%s_%s_%d.png" % (tag, name, len(ops)))
                _clut(mg, tables, clut)
                ops.append((clut, mask))

    if not ops:
        return {"ok": True, "changed": False, "findings": findings,
                "warnings": warnings}

    # 16 бит намеренно: правок на кадре бывает несколько, и в восьми битах
    # они складываются в рваные переходы — это ловилось на реальном номере.
    _apply_all(mg, src, ops, dst, 16,
               expand=float(opts.get("mask_expand", 1.0)),
               feather=float(opts.get("mask_feather", 1.5)))
    return {"ok": True, "changed": True, "findings": findings,
            "warnings": warnings}


# --------------------------------------------------------------------------- #
#  Общий тон без Adobe: чёрная, серая и белая точки
# --------------------------------------------------------------------------- #
#
# Перенос correctTone из color_correct.jsx, слово в слово по смыслу. Зачем
# понадобился: Core Image точку белого НЕ ставит вовсе — он делает баланс
# по лицам, живость цвета, кривую тона и тени. Замер на реальном номере:
# у кадра с тёплым уводом света стояли 244/237/224 (R минус B = +20); Adobe
# свёл их в 240/239/240, то есть увод снял начисто, а вариант без Adobe
# оставил 246/236/226 — не тронул. Именно это и видно глазом как «у Adobe
# натуральнее».
#
# Работает в паре: светлоту (контраст, тени) берём у Core Image, цвет —
# отсюда. Две половины делают ровно то, что каждая умеет лучше.

def _share_above(mg, img, pct, mask=None):
    """Доля пикселей ярче порога; с маской — доля внутри маски."""
    cmd = [mg, str(img), "-threshold", "%.4f%%" % pct]
    if mask is not None:
        cmd += [str(mask), "-compose", "Multiply", "-composite"]
    cmd += ["-format", "%[fx:mean]", "info:"]
    try:
        return float(_run(cmd).strip())
    except Exception:
        return 0.0


def _percentile(mg, img, share, mask=None, mask_area=None, iters=9):
    """
    Порог (в процентах 0..100), выше которого лежит доля `share` пикселей.

    Перцентиля у magick нет, зато есть порог и среднее: двоичным поиском
    девяти шагов хватает на точность в полпроцента, а это заметно меньше,
    чем разброс самого замера.
    """
    want = share * (mask_area if mask is not None else 1.0)
    lo, hi = 0.0, 100.0
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if _share_above(mg, img, mid, mask) > want:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0 * 2.55


def _tanh(x):
    if x > 20:
        return 1.0
    if x < -20:
        return -1.0
    e = math.exp(2.0 * x)
    return (e - 1.0) / (e + 1.0)


def _clamp_shift(frm, to, limit, strength=1.0):
    """Мягкое колено: мелкий сдвиг проходит целиком, крупный насыщается."""
    if limit <= 0:
        return frm
    d = (to - frm) * strength
    return frm + limit * _tanh(d / limit)


def _split_target(frm, goal_avg, avg_from, limit, color_limit, strength, color_strength):
    """Светлота и цвет правятся с РАЗНОЙ силой — у них разная цена ошибки."""
    light = limit * _tanh(((goal_avg - avg_from) * strength) / limit) if limit > 0 else 0.0
    color = (color_limit * _tanh(((avg_from - frm) * strength * color_strength) / color_limit)
             if color_limit > 0 else 0.0)
    return frm + light + color


def _interp_table(table, x):
    """Значение таблицы в дробной точке."""
    x = max(0.0, min(255.0, x))
    i = int(x)
    j = min(255, i + 1)
    return table[i] + (table[j] - table[i]) * (x - i)


def _extrapolated_top(points):
    """
    Таблица 256 значений: низ закреплён опорными точками, верх открыт.

    Открытый верх нужен, чтобы сдвинуть света (кривая, приходящая в (255,255),
    этого не умеет). Открытый НИЗ не нужен и вреден: он отсекает всё, что
    темнее нижней опоры. Поэтому ниже первой точки идём по её же отрезку,
    а выше последней — продолжаем наклоном и упираемся в 255.
    """
    pts = [p for p in points if p[0] is not None]
    if len(pts) < 2:
        return _identity()
    out = []
    for i in range(256):
        x = float(i)
        if x <= pts[0][0]:
            x0, y0 = pts[0]
            x1, y1 = pts[1]
        elif x >= pts[-1][0]:
            x0, y0 = pts[-2]
            x1, y1 = pts[-1]
        else:
            j = 1
            while j < len(pts) and x > pts[j][0]:
                j += 1
            x0, y0 = pts[j - 1]
            x1, y1 = pts[j]
        k = (y1 - y0) / (x1 - x0) if x1 != x0 else 1.0
        out.append(max(0.0, min(255.0, y0 + (x - x0) * k)))
    return out


def _tone_points(lo, lo_to, mid, mid_to, hi, hi_to):
    """Кривая по трём точкам, отрезками. Совпавшие с целью точки не двигаются."""
    pts = [(0.0, 0.0)]

    def add(x, y):
        x = max(0.0, min(255.0, x))
        y = max(0.0, min(255.0, y))
        if x <= pts[-1][0]:
            return
        pts.append((x, y))

    px, py = 0.0, 0.0
    for x1, y1 in ((lo, lo_to), (mid, mid_to), (hi, hi_to)):
        if x1 is None or x1 <= px:
            continue
        add(x1, y1)
        px, py = x1, y1
    add(255.0, 255.0)
    return pts


def correct_tone(job, work, chans, size, sky_mask=None, skin_mask=None):
    """
    Три пипетки по околонейтральным пикселям. Возвращает (таблицы или None, текст).

    Белая точка берётся по НЕЙТРАЛЯМ, а не по самым ярким пикселям кадра:
    пипетка ретушёра тычет в белую рубашку, а не в блик на бокале. Яркие
    пиксели могут быть насыщенным цветом — жёлтая стена в солнце, — и сведение
    их каналов к общей цели выбелило бы сам цвет.

    Небо из нейтралей ВЫЧИТАЕТСЯ. Бледное небо лежит в той же коробке Lab
    (b около -10 при допуске 15) и на пейзаже занимает полкадра, становясь
    «эталоном серого»; серая точка после этого обесцвечивает само небо —
    ловилось как уход 210/220/226 в сиреневое 207/214/229.
    """
    mg = job["magick"]
    t = job.get("tone") or {}
    workdir = Path(job["workdir"])
    tag = str(job["tag"])

    nb = t.get("neutral_box") or [45, 97, 15]
    db = t.get("dark_box") or [4, 34, 12]
    neutral = _lab_box(mg, chans, [nb[0], -nb[2], -nb[2]], [nb[1], nb[2], nb[2]],
                       workdir, tag + "_ntr")
    if sky_mask is not None:
        neutral = _subtract(mg, neutral, sky_mask, workdir / ("ntr_nosky_%s.png" % tag))
    # КОЖУ вычитаем обязательно — по той же причине, что и небо, только цена
    # ошибки выше. Бледная кожа лежит ВНУТРИ коробки нейтралей: замер того
    # кадра, где ноги вышли трупно-серыми, — лица L 57.9, a 13.0, b 6.2 при
    # коробке L 45-97, |a| и |b| до 15. Кожа составляла 34% всей маски
    # нейтралей, то есть треть «эталона серого» была ногами девушек, и серая
    # точка их честно обесцвечивала. Серый баланс по коже не ставят никогда.
    if skin_mask is not None:
        neutral = _subtract(mg, neutral, skin_mask, workdir / ("ntr_noskin_%s.png" % tag))
    area = _area(mg, neutral)
    if area < float(t.get("neutral_min_area_percent", 1.5)):
        return None, ("neutrals cover %.1f%% of the frame, below the %.1f%% threshold — colour untouched"
                      % (area, float(t.get("neutral_min_area_percent", 1.5))))

    nmean = _masked_mean_rgb(mg, work, neutral, workdir, tag + "n")
    if nmean is None:
        return None, "neutrals could not be measured"

    # Светлая часть нейтралей — по ней ставится белая точка.
    share = _area(mg, neutral) / 100.0
    lchan = chans[0]
    thr = _percentile(mg, lchan, 1.0 - float(t.get("neutral_highlight_percentile", 95)) / 100.0,
                      neutral, share)
    hi_mask = workdir / ("ntrhi_%s.png" % tag)
    _run([mg, str(lchan), "-threshold", "%.4f%%" % (thr / 2.55), str(neutral),
          "-compose", "Multiply", "-composite", str(hi_mask)])
    nhi = _masked_mean_rgb(mg, work, hi_mask, workdir, tag + "h")
    if nhi is None:
        nhi = list(nmean)

    # Откуда брать белую точку — развилка со вкусом, а не с правильным ответом.
    #
    # "neutrals" — по околонейтральным пикселям: пипетка ретушёра тычет
    # в белую рубашку, а не в блик. Бережно, но на кадре, где самое светлое
    # НЕ нейтраль (залитый солнцем деревянный навес), увод почти не снимается:
    # такие пиксели лежат выше опорной точки, где кривая уже сходит на нет.
    # Замер: тёплый кадр с R минус B +20 этим способом дотягивался только до +7.
    #
    # "image" — по собственным светам кадра, поканально. Ровно то, что делает
    # autoBlackWhite у Adobe, и ровно то, что на реальном номере выглядит
    # натуральнее: тот же кадр выходит в −1. Платой может быть выбеливание
    # насыщенного цвета, если он и есть самое светлое в кадре.
    by_levels = str(t.get("white_point_from", "image")).lower() == "image"
    use_gray = bool(t.get("gray_point", True))
    if by_levels:
        q = float(t.get("highlight_percentile", 0.5)) / 100.0
        by_image = [_percentile(mg, chans[0] if False else ch, q)
                    for ch in _rgb_channels(mg, work, workdir, tag)]
        if all(v > 0 for v in by_image):
            nhi = by_image

    # Чёрная точка — ТОЛЬКО по тёмным нейтралям. Поканальный перцентиль всего
    # кадра задают не тени, а насыщенные предметы: красные брюки давали
    # «тень» 27/3/2, и сведение её к нейтрали озеленяло кожу в тени.
    dark = _lab_box(mg, chans, [db[0], -db[2], -db[2]], [db[1], db[2], db[2]],
                    workdir, tag + "_drk")
    if sky_mask is not None:
        dark = _subtract(mg, dark, sky_mask, workdir / ("drk_nosky_%s.png" % tag))
    if skin_mask is not None:
        dark = _subtract(mg, dark, skin_mask, workdir / ("drk_noskin_%s.png" % tag))
    dark_area = _area(mg, dark)
    lo = None
    # Чёрную точку по умолчанию НЕ ТРОГАЕМ.
    #
    # Просили снять увод в СВЕТАХ — это белая и серая точки. Чёрная точка
    # к этому ничего не добавляет, а ломает много: среднее по тёмным нейтралям
    # бывает и 54 из 255, и сведение его к цели 10 сжимает весь нижний
    # диапазон. На реальном номере это дало серые и чёрные ноги, синие пятна
    # в тенях одежды и пятнистые штаны. Включать осознанно и на подходящем
    # материале.
    if (bool(t.get("shadow_points", False))
            and dark_area >= float(t.get("dark_min_area_percent", 0.5))):
        lo = _masked_mean_rgb(mg, work, dark, workdir, tag + "d")
    # Чёрную точку по ВСЕМУ КАДРУ не берём никогда, даже в режиме уровней:
    # поканальный перцентиль задают не тени, а насыщенные предметы (красные
    # брюки давали «тень» 27/3/2). Нет тёмных нейтралей — не трогаем тени вовсе.

    hi_target = float(t.get("highlight_target", 248))
    lo_target = float(t.get("shadow_target", 10))
    limit = float(t.get("max_shift", 18))
    lo_limit = float(t.get("shadow_max_shift", 8))
    color_limit = float(t.get("color_max_shift", 12))
    strength = float(t.get("strength", 1.0))
    cs = float(t.get("color_strength", 0.6))
    min_move = float(t.get("min_move", 3))

    # Цель белой точки не ниже того, что уже есть: опускать света до цели
    # значит класть краску туда, где чистая бумага.
    hi_goal = max(hi_target, max(nhi))
    # И не выше, чем позволяют собственные света кадра: кривая действует
    # на всё, и лежащее выше опоры прижимается к потолку. На кадре с облаками
    # подъём опоры с 220 до 239 сжал облака в белое пятно.
    ceiling = float(t.get("highlight_ceiling", 252))
    img_hi = _percentile(mg, work, float(t.get("highlight_percentile", 0.5)) / 100.0)
    hi_avg = sum(nhi) / 3.0
    room = ceiling - img_hi
    if room < hi_goal - hi_avg:
        hi_goal = hi_avg + max(0.0, room)

    lo_avg = sum(lo) / 3.0 if lo else 0.0
    lo_goal = min(lo_target, lo_avg) if lo else 0.0

    hi_to, lo_to, mid_to = [], [], []
    navg = sum(nmean) / 3.0
    touched = False
    for c in range(3):
        hi_to.append(_split_target(nhi[c], hi_goal, hi_avg, limit, color_limit, strength, cs))
        # В тенях цветовую часть держим строже, чем в светах: там сидит свет
        # сюжета. Требование нейтрали вынимало из тёплых теней контрового
        # света красное с зелёным и оставляло синее — чёрный становился синим,
        # а тени одежды шли пятнами. Предел тот же, что у светлоты теней.
        lo_color_limit = min(color_limit, lo_limit)
        lo_to.append(_split_target(lo[c], lo_goal, lo_avg, lo_limit,
                                   lo_color_limit, strength, cs)
                     if lo else 0.0)
        # Серая точка — операция чисто цветовая, светлоты в ней нет.
        mid_to.append(_clamp_shift(nmean[c], navg, color_limit, strength * cs))
        if (abs(hi_to[c] - nhi[c]) >= min_move
                or (lo and abs(lo_to[c] - lo[c]) >= min_move)
                or abs(mid_to[c] - nmean[c]) >= min_move):
            touched = True
    if not touched:
        return None, "neutrals %.1f%% of the frame, the cast is under the threshold — untouched" % area

    if by_levels:
        # Уровни: три опорные точки, но концы НЕ закреплены — за крайними
        # точками кривая продолжается тем же наклоном и упирается в 0 и 255.
        #
        # Именно закреплённые концы и мешали повторить Adobe, сколько ни
        # поднимай пределы: правка сходила на нет ровно там, где нужна, —
        # в светах. А серую точку выбрасывать нельзя: Adobe делает ДВА
        # действия, autoBlackWhite и autoNeutrals, и нейтрализует основную
        # массу кадра как раз вторым. Без неё тёплый кадр с R минус B +20
        # дотягивался только до +13 вместо −1.
        tables = []
        for c in range(3):
            # НИЗ ЗАКРЕПЛЁН В (0,0), открыт только верх.
            #
            # Снимать закрепление с обоих концов нельзя: всё, что темнее
            # нижней опоры, уходит в ноль. У Photoshop чёрная точка — это
            # нижний перцентиль, доли процента пикселей; сюда же подставляется
            # СРЕДНЕЕ по тёмным нейтралям, а оно бывает и 55 из 255, то есть
            # средне-серым. На реальном номере это схлопнуло весь нижний
            # диапазон: ноги вышли чёрными и серыми, тени одежды — синими.
            pts = [(0.0, 0.0)]
            if lo:
                pts.append((lo[c], lo_to[c]))
            pts.append((nhi[c], hi_to[c]))
            pts = sorted(set(pts), key=lambda q: q[0])
            tbl = _extrapolated_top(pts)
            # Серая точка — ГАММА канала, а не опорная точка на кривой.
            #
            # В этом и была последняя нестыковка с Adobe. Точка на кривой
            # правит окрестность середины и сходит на нет к светам; гамма
            # двигает ВЕСЬ диапазон. Именно поэтому autoNeutrals у Photoshop
            # вытягивает тёплый кадр в нейтраль целиком, а наша трёхточечная
            # кривая дотягивала только до R минус B +11 вместо −1.
            here = _interp_table(tbl, nmean[c])
            want = _interp_table(tbl, nmean[c]) + (mid_to[c] - nmean[c])
            # gray_point = false отключает гамму: остаётся только белая точка.
            # Разница по действию велика. Белая точка работает по светам,
            # а гамма двигает ВСЮ середину диапазона — там, где живёт кожа.
            # Даже с вычтенной из эталона кожей гамма оставляла ноги бледнее
            # оригинала, а без неё загар сохраняется.
            if not use_gray:
                pass
            elif 1.0 < here < 254.0 and 1.0 < want < 254.0:
                g = math.log(want / 255.0) / math.log(here / 255.0)
                g = max(0.5, min(2.0, g))     # дальше это уже не тон, а порча
                tbl = [255.0 * ((v / 255.0) ** g) if v > 0 else 0.0 for v in tbl]
            tables.append(tbl)
    else:
        tables = [_table(_tone_points(lo[c] if lo else None, lo_to[c] if lo else None,
                                      nmean[c] if use_gray else None,
                                      mid_to[c] if use_gray else None,
                                      nhi[c], hi_to[c]))
                  for c in range(3)]
    r = lambda v: "/".join(str(int(round(x))) for x in v)
    note = ("neutrals %.1f%% of the frame; white point %s -> %s" % (area, r(nhi), r(hi_to)))
    note += (", серая %s -> %s" % (r(nmean), r(mid_to)) if use_gray
             else ", grey point disabled")
    note += (", чёрная по тёмным нейтралям %s -> %s" % (r(lo), r(lo_to))
             if lo else ", no dark neutrals — black point untouched")
    return tables, note


def lift(job):
    """
    Вытягивание недодержанного кадра: экспозиция, тени, насыщенность —
    ПО ВСЕМУ КАДРУ. Ответ той же формы, что у tone(): (путь или None, текст).

    ПОЧЕМУ НЕ ПО МАСКЕ ЧЕЛОВЕКА, хотя сначала было так. Маска даёт ОРЕОЛ:
    правка сильная, край мягкий, и вокруг фигуры появляется светящаяся
    кайма — на контровом кадре её видно сразу. Сверх того маска не лечит
    того, ради чего бралась: ноги в тени остаются синеватыми, потому что
    подъём светлоты не трогает цвет. Ретушёр в этом месте делает другое —
    поднимает экспозицию целиком, потом тени, потом насыщенность; порядок
    отсюда и взят.

    ПОЧЕМУ ЭКСПОЗИЦИЯ ГАММОЙ, а не умножением. Гамма закрепляет концы:
    белое остаётся белым, и небо с водой не выбиваются в бумагу. Умножение
    в линейном свете физичнее, но на кадре с выжженным фоном оно срезает
    света начисто.

    ПОЧЕМУ СИЛА БЕРЁТСЯ ПО КОЖЕ, а не по кадру. У ткани отражение любое:
    тёмно-синее поло даёт низкую яркость при совершенно правильной
    экспозиции. У кожи отражение известно — тёмной она бывает только от
    нехватки света. Замер на кадрах номера: нормально снятые 0.44…0.53,
    контровый закат 0.26, групповое фото с лицами в тени козырьков 0.17.
    Разрыв почти вдвое, без перекрытия.

    ЯРЛЫКА «КОНТРОВЫЙ СВЕТ» ЗДЕСЬ НЕТ НАМЕРЕННО. Определить его не выходит:
    три метрики — фон ярче человека, лицо темнее кадра, контур ярче тела —
    на 44 кадрах номера ставили наверх обычные снимки под ярким небом, а
    настоящий контровый кадр оказывался ниже них. Помеха структурная:
    в спортивной съёмке яркий фон — норма. Этап и не спрашивает, откуда свет;
    он спрашивает, хватило ли его коже.
    """
    mg = job["magick"]
    src = Path(job["src"])
    workdir = Path(job["workdir"])
    tag = str(job["tag"])
    opts = job.get("lift") or {}
    cs = str(job.get("colorspace") or "").lower()
    # То же ограничение, что у общего тона: в CMYK замер яркости означал бы
    # не то, а гамма легла бы в краску, а не в свет.
    if "rgb" not in cs:
        return None, "mode %s — untouched" % (cs or "?")
    if not skin_model_available():
        return None, "the skin model is not installed"
    _set_skin_limits(job.get("native") or {})

    detect_px = int(job.get("detectPx", 2000))
    work = workdir / ("lwork_%s.miff" % tag)
    _run([mg, str(src) + "[0]", "-resize", "%dx%d>" % (detect_px, detect_px),
          "-alpha", "off", "-depth", "16", str(work)])
    size = _size(mg, work)

    skin = _skin_model_mask(mg, src, size, workdir, tag + "l")
    if skin is None:
        # Та же причина, что и у памятных цветов: если маске нельзя верить,
        # то и яркость кожи по ней замерена неверно, а по ней тут всё решается.
        if _LAST_SKIN_DISAGREE[0] > 0:
            return None, ("the human-parsing models disagree %.1ffold — "
                          "the skin measurement is not trustworthy"
                          % _LAST_SKIN_DISAGREE[0])
        return None, "the skin model did not run"
    if _LAST_SKIN_NO_PEOPLE[0] >= 0:
        return None, ("no people in the frame (no faces, no figures, people label %.2f)"
                      % _LAST_SKIN_NO_PEOPLE[0])
    area = _area(mg, skin)
    if area < float(opts.get("min_skin_percent", 0.3)):
        # Кожи в кадре почти нет — мерить нечего, а догадка по одежде и есть
        # то, чего этот этап избегает.
        return None, "skin is %.2f%% of the frame — too little to judge" % area

    gray = workdir / ("lgray_%s.miff" % tag)
    _run([mg, str(work), "-colorspace", "Gray", str(gray)])
    lit = workdir / ("llitm_%s.miff" % tag)
    _multiply(mg, gray, skin, lit)
    lum = _fx(mg, lit, "mean") / max(1e-6, area / 100.0)

    target = float(opts.get("skin_target", 0.42))
    floor = float(opts.get("skin_floor", 0.18))
    if lum >= target:
        return None, "skin %.3f, norm from %.2f — exposed, nothing to lift" % (lum, target)

    # Мягкое колено, но НОРМИРОВАННОЕ: голый tanh(1) = 0.76, то есть кадр,
    # упавший ровно до floor, получал три четверти хода вместо полного —
    # предел был назначен, но недостижим. Замер на кадре с рукопожатием:
    # кожа 0.186 при floor 0.18 давала силу 0.75 и экспозицию 1.34 там, где
    # ретушёр руками ставил заметно больше. Делим на tanh(k), чтобы дно
    # шкалы означало именно предел; колено при этом остаётся — оно и нужно
    # в середине, где недодержка мелкая.
    span = max(1e-6, target - floor)
    k = 1.6
    strength = min(1.0, max(0.0, _tanh(k * (target - lum) / span) / _tanh(k)))
    expo = 1.0 + (float(opts.get("max_exposure", 1.45)) - 1.0) * strength
    shad = 1.0 + (float(opts.get("max_shadow", 1.7)) - 1.0) * strength
    sat = 100.0 + float(opts.get("max_saturation", 25.0)) * strength
    note = ("skin %.3f (norm from %.2f): exposure %.2f, shadows %.2f, saturation %+d%%"
            % (lum, target, expo, shad, int(round(sat - 100))))
    if not job.get("apply", True):
        return None, note + " — measure only"

    # 1. Экспозиция по всему кадру. 2. Тени — та же гамма, но подмешанная
    #    по маске «где темно», чтобы света не поехали второй раз.
    #    3. Насыщенность: тени теряют цвет, и поднятая светлота сама по себе
    #    оставляет их сизыми — синеватые ноги ровно отсюда.
    base = workdir / ("lbase_%s.miff" % tag)
    _run([mg, str(src) + "[0]", "-gamma", "%.3f" % expo, str(base)])
    bg = workdir / ("lbgray_%s.miff" % tag)
    _run([mg, str(base), "-colorspace", "Gray", str(bg)])
    smask = workdir / ("lsm_%s.miff" % tag)
    _run([mg, str(bg), "-negate", "-blur", "0x%g" % float(opts.get("shadow_softness", 15)),
          "-sigmoidal-contrast", "4,50%%", str(smask)])
    shadowed = workdir / ("lshad_%s.miff" % tag)
    _run([mg, str(base), "-gamma", "%.3f" % shad, str(shadowed)])
    dst = workdir / ("lift_%s%s" % (tag, src.suffix or ".tif"))
    _run([mg, str(base), str(shadowed), str(smask), "-compose", "over", "-composite",
          "-modulate", "100,%.1f,100" % sat, str(dst)])
    if not dst.exists():
        return None, note + " — magick wrote nothing"
    return dst, note


def tone(job):
    """
    Общий тон без Adobe: подготовка, замер, правка. Ответ той же формы,
    что у run_core_image_tone — (путь или None, текст).

    Небо считается ДО нейтралей и вычитается из них: так требует инвариант,
    ради которого в JSX заводилась вся возня с масками.
    """
    mg = job["magick"]
    src = Path(job["src"])
    workdir = Path(job["workdir"])
    tag = str(job["tag"])
    opts = job.get("native") or {}
    cs = str(job.get("colorspace") or "").lower()
    if "rgb" not in cs:
        return None, "mode %s — the overall tone is untouched" % (cs or "?")

    detect_px = int(job.get("detectPx", 2000))
    work = workdir / ("twork_%s.miff" % tag)
    _run([mg, str(src) + "[0]", "-resize", "%dx%d>" % (detect_px, detect_px),
          "-alpha", "off", "-depth", "16", str(work)])
    size = _size(mg, work)
    chans = _lab_channels(mg, work, workdir, tag + "t")

    sky = None
    try:
        box = _lab_box(mg, chans, opts.get("sky_lab_min") or [35, -40, -75],
                       opts.get("sky_lab_max") or [100, 12, -3], workdir, tag + "_tsky")
        if _area(mg, box) >= 3.0:
            sky = box
    except Exception:
        sky = None

    # Маска кожи для вычитания из нейтралей. Строится тем же способом, что
    # и в памятных цветах: коробка Lab вокруг замеренного цвета лица внутри
    # маски человека.
    skin = None
    try:
        vis = _vision(src, workdir, tag + "t", detect_px)
        person = _person_mask(mg, vis, size, workdir, tag + "t")
        faces = vis.get("faces") or []
        if person is not None and faces:
            tol = opts.get("skin_tolerance") or [45, 12, 18]
            for i, face in enumerate(faces):
                ref = _face_reference(mg, work, face, size, workdir, tag)
                box = _lab_box(mg, chans,
                               [ref[0] - tol[0], ref[1] - tol[1], ref[2] - tol[2]],
                               [ref[0] + tol[0], ref[1] + tol[1], ref[2] + tol[2]],
                               workdir, "%s_ts%d" % (tag, i))
                one = workdir / ("tskin_%s_%d.png" % (tag, i))
                _multiply(mg, box, person, one)
                if skin is None:
                    skin = one
                else:
                    both = workdir / ("tskinu_%s_%d.png" % (tag, i))
                    _run([mg, str(skin), str(one), "-compose", "Lighten",
                          "-composite", str(both)])
                    skin = both
            if skin is not None:
                # С запасом: по краю кожи градиент, и полупопадания в эталон
                # серого тоже не нужны.
                grown = workdir / ("tskin_%s_big.png" % tag)
                _run([mg, str(skin), "-morphology", "Dilate", "Disk:3",
                      "-blur", "0x2", "-threshold", "20%", str(grown)])
                skin = grown
    except Exception:
        skin = None

    tables, note = correct_tone(job, work, chans, size, sky, skin)
    if tables is None:
        return None, note
    if not job.get("apply", True):
        return None, note + " (no correction applied)"
    clut = workdir / ("tclut_%s.png" % tag)
    _clut(mg, tables, clut)
    dst = workdir / ("tone_%s.tif" % tag)
    full = workdir / ("tfull_%s.png" % tag)
    w, h = _size(mg, src)
    _run([mg, "-size", "%dx%d" % (w, h), "xc:white", str(full)])
    # Маска здесь — белый кадр целиком, краю неоткуда взяться.
    _apply_all(mg, src, [(clut, full)], dst, 16, expand=0, feather=0)
    return dst, note
