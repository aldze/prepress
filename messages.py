#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сообщения прогона: код плюс числа вместо готовой прозы.

Зачем. Раньше причины и предупреждения складывались в манифест строками
по-русски, и отчёт их просто показывал. Из-за этого английский отчёт выходил
наполовину русским: шапки переведены, а в ячейках «сюжет не подтверждён»
и «область 0.1% кадра, меньше порога 0.5%». Перевести это на месте нельзя —
текст уже собран и числа в нём замурованы.

Поэтому пайплайн кладёт в манифест КОД и ЧИСЛА:

    {"code": "ppi_high", "params": {"ppi": 1241.0, "max": 350}}

а текст собирается при показе — на том языке, который попросили. Журнал
при этом английский всегда: он читается при разборе сбоя, и один язык
диагностики дешевле двух.

Старые строки продолжают работать: render() отдаёт строку как есть. Это
нужно, чтобы переезжать по частям, а не одним махом, — предупреждения
из JSX пока приходят прозой.
"""

# --------------------------------------------------------------------------- #
#  Словарь
# --------------------------------------------------------------------------- #
#
# Ключ — код, значение — шаблоны по языкам. Параметры подставляются по имени:
# так шаблон можно переписать, не трогая порядок аргументов, и пропущенный
# параметр виден сразу, а не превращается в «%s» посреди отчёта.
CATALOG = {
    # --- цвет ---
    "cmyk_no_icc": {
        "en": "CMYK without an embedded profile",
        "ru": "CMYK без встроенного профиля"},
    "cmyk_no_icc_warn": {
        "en": "no ICC profile in the file; the source profile is assumed equal to the "
              "target (no conversion, the profile is assigned)",
        "ru": "у файла нет ICC-профиля; исходный профиль принят равным целевому "
              "(конверсия не выполняется, профиль присваивается)"},
    "cmyk_foreign_icc": {
        "en": "CMYK with a foreign profile: \"{icc}\"",
        "ru": "CMYK с чужим профилем: «{icc}»"},
    "gray_to_cmyk": {
        "en": "Grayscale → CMYK via \"{icc}\"",
        "ru": "Grayscale → CMYK через «{icc}»"},
    "gray_kept": {
        "en": "Grayscale left as is (convert_grayscale_to_cmyk = false)",
        "ru": "Grayscale оставлен как есть (convert_grayscale_to_cmyk = false)"},
    "not_cmyk": {
        "en": "not CMYK: {cs}{icc}",
        "ru": "не CMYK: {cs}{icc}"},
    "to_rgb": {
        "en": "{cs} → RGB via \"{icc}\"",
        "ru": "{cs} → RGB через «{icc}»"},
    "rgb_foreign_icc": {
        "en": "RGB with a foreign profile: \"{icc}\"",
        "ru": "RGB с чужим профилем: «{icc}»"},
    "rgb_no_icc": {
        "en": "RGB without an embedded profile",
        "ru": "RGB без встроенного профиля"},

    # --- разрешение ---
    "ppi_unknown": {
        "en": "InDesign did not report an effective PPI — resampling skipped",
        "ru": "InDesign не сообщил effective PPI — ресемплинг пропущен"},
    "ppi_high": {
        "en": "effective PPI {ppi:.1f} > {max:.0f}",
        "ru": "effective PPI {ppi:.1f} > {max:.0f}"},
    "ppi_low_ai": {
        "en": "effective PPI {ppi:.1f} < {min:.0f} — AI upscale",
        "ru": "effective PPI {ppi:.1f} < {min:.0f} — ИИ-апскейл"},
    "ppi_low_ai_warn": {
        "en": "AI UPSCALE: {ppi:.1f} → {target:.0f} ppi. The model invents detail — "
              "review the result, especially small text, badges and logos",
        "ru": "ИИ-АПСКЕЙЛ: {ppi:.1f} → {target:.0f} ppi. Модель додумывает детали — "
              "просмотрите результат, особенно мелкий текст, шильдики и логотипы"},
    "ppi_low_no_ai": {
        "en": "effective PPI {ppi:.1f} < {min:.0f} — AI upscale unavailable ({why})",
        "ru": "effective PPI {ppi:.1f} < {min:.0f} — ИИ-апскейл недоступен ({why})"},
    "skin_mask_untrusted": {
        "en": "SKIN NOT CORRECTED: the two human-parsing models disagree "
              "{ratio:.1f}fold ({big:.1f}% against {small:.1f}% of the frame) — "
              "the mask is not trustworthy. On such frames the correction lands "
              "in bands: part of a limb is inside the mask, the next part is not. "
              "Typical cause — many small people in one frame",
        "ru": "КОЖА НЕ ПРАВЛЕНА: модели разбора человека разошлись в {ratio:.1f} раза "
              "({big:.1f}% против {small:.1f}% кадра) — маске нельзя верить. "
              "На таких кадрах правка ложится полосами: часть голени попадает "
              "в маску, соседняя нет. Обычная причина — много мелких людей в кадре"},
    "ppi_low_no_ai_warn": {
        "en": "LOW RESOLUTION: effective PPI {ppi:.1f} < {min:.0f}, AI upscale not "
              "applied ({why})",
        "ru": "НИЗКОЕ РАЗРЕШЕНИЕ: effective PPI {ppi:.1f} < {min:.0f}, ИИ-апскейл "
              "не применён ({why})"},
    "ppi_below": {
        "en": "effective PPI {ppi:.1f} below {below:.0f}",
        "ru": "effective PPI {ppi:.1f} ниже {below:.0f}"},
    "ppi_below_warn": {
        "en": "interpolated upscale {ppi:.1f} → {target:.0f} ppi: no detail is added, "
              "only formal compliance with 300 ppi",
        "ru": "увеличение {ppi:.1f} → {target:.0f} ppi интерполяцией: детали "
              "не добавятся, только формальное соответствие 300 ppi"},
    "ppi_mixed": {
        "en": "several placements at different scales (effective PPI {min:.1f} to "
              "{max:.1f}); the factor follows the weakest one so none drops below "
              "{target:.0f} ppi",
        "ru": "несколько размещений с разным масштабом (effective PPI от {min:.1f} "
              "до {max:.1f}); коэффициент взят по самому «слабому», чтобы ни одно "
              "не упало ниже {target:.0f} ppi"},

    # --- этапы выключены цепочкой ---
    "stage_off_color": {
        "en": "colour conversion skipped: the stage is not in the chain",
        "ru": "цветоделение не делается: этапа нет в цепочке"},
    "stage_off_resample": {
        "en": "resampling skipped: the stage is not in the chain",
        "ru": "ресемплинг не делается: этапа нет в цепочке"},

    # --- плашки и формат ---
    "spots_to_cmyk": {
        "en": "spot channels ({names}) → CMYK",
        "ru": "плашечные каналы ({names}) -> CMYK"},
    "psd_flatten": {
        "en": "the PSD will be flattened when written as TIFF",
        "ru": "PSD будет сведён в один слой при записи TIFF"},
    "non_square_px": {
        "en": "non-square pixels: actual PPI {h:.1f} × {v:.1f}",
        "ru": "неквадратные пиксели: actual PPI {h:.1f} x {v:.1f}"},

    # --- тон ---
    "tone_cmyk_skipped": {
        "en": "overall tone skipped: the source is {cs} — tone is corrected in RGB only",
        "ru": "общий тон пропущен: исходник {cs} — тон правится только в RGB"},
    "tone_dropped_cs": {
        "en": "overall tone dropped: the engine returned {got} instead of {was}",
        "ru": "общий тон отброшен: движок вернул {got} вместо {was}"},
    "tone_failed": {
        "en": "Core Image did not correct the overall tone ({why})",
        "ru": "Core Image не поправил общий тон ({why})"},
    "tone_unlimited": {
        "en": "tone not limited ({why}) — the engine was applied in full",
        "ru": "тон не ограничен ({why}) — движок применён целиком"},

    # --- сторож на выходе ---
    "wrong_colorspace": {
        "en": "the file was written as {got} but must be {want} — the profile "
              "\"{icc}\" is attached, the colour is not converted. Do not send it on",
        "ru": "файл записан как {got}, хотя должен быть {want} — профиль «{icc}» "
              "наклеен, но цвет не переведён. Так отдавать нельзя"},

    # --- памятные цвета ---
    "mem_small": {
        "en": "region {area:.1f}% of the frame, below the {min:.1f}% threshold",
        "ru": "область {area:.1f}% кадра, меньше порога {min:.1f}%"},
    "mem_scene_no": {
        "en": "scene not confirmed (best label {best})",
        "ru": "сюжет не подтверждён (лучший ярлык {best})"},
    "mem_cmyk": {
        "en": "{cs} mode — memory colours are not looked for",
        "ru": "режим {cs} — памятных цветов не ищем"},
    "mem_no_people": {
        "en": "no people in the frame: no faces, no figures, people label {label:.2f}",
        "ru": "людей в кадре нет: ни лиц, ни фигур, ярлык «люди» {label:.2f}"},
    "mem_no_skin_model": {
        "en": "the model found no skin although the frame has {faces} face(s)",
        "ru": "модель не нашла кожи, хотя лиц в кадре {faces}"},

    # --- области ---
    "person_n": {"en": "person {n}", "ru": "человек {n}"},
    "scene_none": {"en": "none", "ru": "ни одного"},
}


def msg(code, **params):
    """Сообщение: код и числа. В манифест уходит как есть."""
    out = {"code": code}
    if params:
        out["params"] = params
    return out


def render(item, lang="en"):
    """
    Сообщение -> текст на нужном языке.

    Строку отдаёт как есть: пока часть предупреждений приходит прозой из JSX,
    и ломать их ради переезда незачем. Неизвестный код показывается кодом —
    это видно в отчёте и чинится добавлением строки в словарь, а не падением
    прогона посреди номера.
    """
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
    code = item.get("code")
    if not code:
        return str(item.get("text") or "")
    entry = CATALOG.get(code)
    if not entry:
        return code
    template = entry.get(lang) or entry.get("en") or entry.get("ru") or code
    params = dict(item.get("params") or {})
    # Вложенные сообщения (например, «лучший ярлык ни одного») разворачиваем
    # на том же языке — иначе внутри английской фразы окажется русское слово.
    for k, v in list(params.items()):
        if isinstance(v, dict) and "code" in v:
            params[k] = render(v, lang)
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        # Шаблон и параметры разошлись. Отчёт при этом не должен рассыпаться:
        # показываем код с числами, чтобы было видно, что чинить.
        return "%s %s" % (code, params or "")


def render_all(items, lang="en"):
    """Список сообщений -> список строк."""
    return [render(x, lang) for x in (items or [])]
