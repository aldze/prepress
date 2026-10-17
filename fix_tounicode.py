#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дописывает /ToUnicode в шрифты готового PDF.

Зачем. PDF не хранит текст в кодировке — в потоке страницы лежат коды глифов
конкретного субсета шрифта. Кириллическая строка «АВЕНЮ» в экспорте InDesign
выглядит как байты \\037\\036\\035… Обратный перевод кода в Unicode делает
табличка /ToUnicode внутри шрифта, и если её нет, копирование текста из
Acrobat даёт белиберду.

InDesign пишет /ToUnicode не всегда. Замер на номере Y123 (224 планшетных
PDF): 855 шрифтов из 877 с табличкой, 22 — без, и ровно эти 10 файлов
не копируются. Переэкспорт проблему не гарантирует (переключателя
«писать ToUnicode» в диалоге нет), поэтому табличка дописывается сюда же,
в готовый файл.

Откуда берутся числа. У простого шрифта (Type1/TrueType/Type3) в /Encoding
лежит таблица кодов на ИМЕНА глифов: `14 /uni044B`, `31 /A`, `44 /comma`.
Имена вида uniXXXX прямо называют кодовую точку, остальные разбираются
по таблице Adobe Glyph List. То есть карта уже в файле — её просто нужно
переложить в тот вид, который читает Acrobat.

Чего инструмент НЕ умеет. Type0 с /Identity-H: там коды — это CID субсета,
а вложенный CFF у Adobe CID-keyed, имён глифов в нём нет вовсе. Восстановить
Unicode неоткуда, такие шрифты только перечисляются в отчёте — их лечит
переэкспорт полосы.

Файл правится приращением (incremental update): исходные байты остаются
как были, в конец дописываются новые объекты и своя таблица ссылок.
Поэтому картинки, шрифты и разметка страниц не пересобираются и испортиться
не могут — в худшем случае новый хвост просто не будет прочитан.

Разбор PDF берётся из check_pdf.py. Это единственная общая деталь между
инструментами проекта: держать второй парсер xref и object streams дороже,
чем нарушить здесь правило «общего кода нет» — оно про то, чтобы не мешать
в одном файле вызовы Adobe и подтверждения, а не про чтение структуры.
"""

import argparse
import re
import sys
import unicodedata
import zlib
from pathlib import Path

from check_pdf import (PDFDocument, PDFParseError, Name, Ref, Stream,
                       decode_stream, iter_pdf_paths)


# --------------------------------------------------------------------------- #
#  Имена глифов -> Unicode
# --------------------------------------------------------------------------- #

# Латинская часть WinAnsiEncoding: код -> имя глифа. Таблица нужна в обе
# стороны — как база кодировки и как источник имён для AGL, — поэтому
# записана один раз, а Unicode для имени добывается декодированием кода
# в cp1252 (WinAnsi это она и есть). Так в таблице не может завестись
# опечатка в кодовой точке: её никто не набирал руками.
_WIN_ANSI_NAMES = {}

def _fill_win_ansi():
    low = ("space exclam quotedbl numbersign dollar percent ampersand quotesingle "
           "parenleft parenright asterisk plus comma hyphen period slash "
           "zero one two three four five six seven eight nine "
           "colon semicolon less equal greater question at").split()
    for i, n in enumerate(low):
        _WIN_ANSI_NAMES[32 + i] = n
    for i in range(26):
        _WIN_ANSI_NAMES[65 + i] = chr(65 + i)
        _WIN_ANSI_NAMES[97 + i] = chr(97 + i)
    for i, n in enumerate("bracketleft backslash bracketright asciicircum underscore grave".split()):
        _WIN_ANSI_NAMES[91 + i] = n
    for i, n in enumerate("braceleft bar braceright asciitilde".split()):
        _WIN_ANSI_NAMES[123 + i] = n
    high = {
        128: "Euro", 130: "quotesinglbase", 131: "florin", 132: "quotedblbase",
        133: "ellipsis", 134: "dagger", 135: "daggerdbl", 136: "circumflex",
        137: "perthousand", 138: "Scaron", 139: "guilsinglleft", 140: "OE",
        142: "Zcaron", 145: "quoteleft", 146: "quoteright", 147: "quotedblleft",
        148: "quotedblright", 149: "bullet", 150: "endash", 151: "emdash",
        152: "tilde", 153: "trademark", 154: "scaron", 155: "guilsinglright",
        156: "oe", 158: "zcaron", 159: "Ydieresis",
        161: "exclamdown", 162: "cent", 163: "sterling", 164: "currency",
        165: "yen", 166: "brokenbar", 167: "section", 168: "dieresis",
        169: "copyright", 170: "ordfeminine", 171: "guillemotleft",
        172: "logicalnot", 174: "registered", 175: "macron", 176: "degree",
        177: "plusminus", 178: "twosuperior", 179: "threesuperior", 180: "acute",
        181: "mu", 182: "paragraph", 183: "periodcentered", 184: "cedilla",
        185: "onesuperior", 186: "ordmasculine", 187: "guillemotright",
        188: "onequarter", 189: "onehalf", 190: "threequarters",
        191: "questiondown", 192: "Agrave", 193: "Aacute", 194: "Acircumflex",
        195: "Atilde", 196: "Adieresis", 197: "Aring", 198: "AE", 199: "Ccedilla",
        200: "Egrave", 201: "Eacute", 202: "Ecircumflex", 203: "Edieresis",
        204: "Igrave", 205: "Iacute", 206: "Icircumflex", 207: "Idieresis",
        208: "Eth", 209: "Ntilde", 210: "Ograve", 211: "Oacute",
        212: "Ocircumflex", 213: "Otilde", 214: "Odieresis", 215: "multiply",
        216: "Oslash", 217: "Ugrave", 218: "Uacute", 219: "Ucircumflex",
        220: "Udieresis", 221: "Yacute", 222: "Thorn", 223: "germandbls",
        224: "agrave", 225: "aacute", 226: "acircumflex", 227: "atilde",
        228: "adieresis", 229: "aring", 230: "ae", 231: "ccedilla",
        232: "egrave", 233: "eacute", 234: "ecircumflex", 235: "edieresis",
        236: "igrave", 237: "iacute", 238: "icircumflex", 239: "idieresis",
        240: "eth", 241: "ntilde", 242: "ograve", 243: "oacute",
        244: "ocircumflex", 245: "otilde", 246: "odieresis", 247: "divide",
        248: "oslash", 249: "ugrave", 250: "uacute", 251: "ucircumflex",
        252: "udieresis", 253: "yacute", 254: "thorn", 255: "ydieresis",
    }
    _WIN_ANSI_NAMES.update(high)

_fill_win_ansi()


def _agl_from_win_ansi():
    out = {}
    for code, name in _WIN_ANSI_NAMES.items():
        out[name] = bytes([code]).decode("cp1252")
    return out

# AGL в объёме, который реально встречается в экспорте Adobe: латиница
# WinAnsi плюс имена из StandardEncoding и MacRomanEncoding, которых в ней
# нет. Полная AGL (≈4300 имён) не нужна: всё, что вне этого набора, Adobe
# называет через uniXXXX, а это разбирается правилом, а не таблицей.
_AGL = _agl_from_win_ansi()
_AGL.update({
    "fraction": u"\u2044", "dotlessi": u"\u0131", "lslash": u"\u0142",
    "Lslash": u"\u0141", "ff": u"\ufb00", "fi": u"\ufb01", "fl": u"\ufb02",
    "ffi": u"\ufb03", "ffl": u"\ufb04", "ogonek": u"\u02db", "ring": u"\u02da",
    "caron": u"\u02c7", "breve": u"\u02d8", "dotaccent": u"\u02d9",
    "hungarumlaut": u"\u02dd", "notequal": u"\u2260", "infinity": u"\u221e",
    "lessequal": u"\u2264", "greaterequal": u"\u2265", "partialdiff": u"\u2202",
    "summation": u"\u2211", "product": u"\u220f", "pi": u"\u03c0",
    "integral": u"\u222b", "Omega": u"\u2126", "radical": u"\u221a",
    "approxequal": u"\u2248", "Delta": u"\u2206", "lozenge": u"\u25ca",
    "apple": u"\uf8ff", "nbspace": u"\u00a0", "sfthyphen": u"\u00ad",
    "minus": u"\u2212", "numero": u"\u2116", "onethird": u"\u2153",
    "twothirds": u"\u2154", "oneeighth": u"\u215b", "threeeighths": u"\u215c",
    "fiveeighths": u"\u215d", "seveneighths": u"\u215e",
})

# Акцентированная латиница (cacute, ecaron, uring, ohungarumlaut, dcroat…)
# строится по правилу, а не таблицей: имя AGL это буква плюс название
# акцента, а то же название есть в имени самого символа Unicode. Таблицей
# пришлось бы выписать сотни строк вручную — и ошибиться в них.
_ACCENT_PHRASES = [
    ("hungarumlaut", "DOUBLE ACUTE"), ("circumflex", "CIRCUMFLEX"),
    ("commaaccent", "COMMA BELOW"), ("dotaccent", "DOT ABOVE"),
    ("dieresis", "DIAERESIS"), ("cedilla", "CEDILLA"), ("macron", "MACRON"),
    ("ogonek", "OGONEK"), ("stroke", "STROKE"), ("croat", "STROKE"),
    ("slash", "STROKE"), ("caron", "CARON"), ("breve", "BREVE"),
    ("acute", "ACUTE"), ("grave", "GRAVE"), ("tilde", "TILDE"),
    ("ring", "RING ABOVE"),
]


def _latin_accented(name):
    for suffix, phrase in _ACCENT_PHRASES:
        if len(name) == len(suffix) + 1 and name.endswith(suffix):
            base = name[0]
            if not ("A" <= base <= "Z" or "a" <= base <= "z"):
                continue
            case = "CAPITAL" if base.isupper() else "SMALL"
            try:
                return unicodedata.lookup(
                    "LATIN %s LETTER %s WITH %s" % (case, base.upper(), phrase))
            except KeyError:
                continue
    return None


_RE_UNI = re.compile(r"^uni((?:[0-9A-Fa-f]{4})+)$")
_RE_U = re.compile(r"^u([0-9A-Fa-f]{4,6})$")


def glyph_name_to_text(name):
    """Имя глифа -> строка Unicode, либо None, если имя ничего не называет.

    Порядок из спецификации AGL: сперва алгоритмические формы uniXXXX и
    uXXXXXX, потом таблица, и только затем отсечение суффикса (`a.sc`,
    `f_i.alt`) — суффикс это начертательный вариант той же буквы.
    """
    m = _RE_UNI.match(name)
    if m:
        pts = [int(m.group(1)[i:i + 4], 16) for i in range(0, len(m.group(1)), 4)]
        # Суррогатная половина сама по себе символом не является: такое имя
        # означает «часть пары», и в bfchar её класть нельзя.
        if any(0xD800 <= p <= 0xDFFF for p in pts):
            return None
        return u"".join(chr(p) for p in pts)
    m = _RE_U.match(name)
    if m:
        p = int(m.group(1), 16)
        if p > 0x10FFFF or 0xD800 <= p <= 0xDFFF:
            return None
        return chr(p)
    if name in _AGL:
        return _AGL[name]
    accented = _latin_accented(name)
    if accented is not None:
        return accented
    if "." in name:
        return glyph_name_to_text(name.split(".", 1)[0])
    # Лигатурная запись `f_i` — тоже часть AGL.
    if "_" in name:
        parts = [glyph_name_to_text(p) for p in name.split("_")]
        if all(parts):
            return u"".join(parts)
    return None


def base_encoding_map(base_name):
    """Имя базовой кодировки -> {код: строка}."""
    out = {}
    if base_name == "MacRomanEncoding":
        for c in range(32, 256):
            try:
                out[c] = bytes([c]).decode("mac_roman")
            except UnicodeDecodeError:
                pass
        return out
    if base_name == "WinAnsiEncoding":
        for c in range(32, 256):
            try:
                ch = bytes([c]).decode("cp1252")
            except UnicodeDecodeError:
                continue  # дыры cp1252: 0x81, 0x8D, 0x8F, 0x90, 0x9D
            out[c] = ch
        # PDF-таблица WinAnsi отличается от cp1252 в двух клетках: неразрывный
        # пробел и мягкий перенос показываются обычными пробелом и дефисом.
        out[0xA0] = u" "
        out[0xAD] = u"-"
        return out
    # StandardEncoding и всё неизвестное: латиница совпадает с WinAnsi всюду,
    # кроме двух кавычек. Верхнюю половину не заполняем — гадать нельзя,
    # непокрытые коды честнее показать в отчёте дырой.
    for c in range(32, 127):
        out[c] = bytes([c]).decode("cp1252")
    if base_name == "StandardEncoding":
        out[39] = u"\u2019"
        out[96] = u"\u2018"
    return out


# --------------------------------------------------------------------------- #
#  Разбор шрифтов документа
# --------------------------------------------------------------------------- #

SIMPLE_SUBTYPES = frozenset(("Type1", "MMType1", "TrueType", "Type3"))

# Дочерние словари составного шрифта. Формально это тоже /Type /Font, но
# /ToUnicode на них не живёт — она принадлежит родительскому Type0, — и
# показывать их в отчёте как «не починили» значит пугать зря.
CID_SUBTYPES = frozenset(("CIDFontType0", "CIDFontType2"))


def iter_font_objects(doc):
    """Все объекты-шрифты документа, по номерам.

    Обход идёт по таблице ссылок, а не по дереву страниц: шрифт может
    сидеть в ресурсах формы, узора или внешнего вида аннотации, и обход
    страниц такой пропускает. Лишнего не наберём — проверяется /Type /Font.
    """
    for num in sorted(doc.xref):
        try:
            obj = doc.get_object(num)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if str(obj.get("Subtype") or "") in CID_SUBTYPES:
            continue
        is_font = str(obj.get("Type") or "") == "Font"
        if not is_font:
            # /Type в словаре шрифта обязателен по спецификации, но если его
            # всё же нет, шрифт узнаётся по паре Subtype + BaseFont.
            if str(obj.get("Subtype") or "") in SIMPLE_SUBTYPES and "BaseFont" in obj:
                is_font = True
        if is_font:
            yield num, obj


def font_code_map(doc, font):
    """Словарь шрифта -> ({код: строка}, [имена, которые не разобрались]).

    Возвращает (None, reason), если карту построить нечем.
    """
    subtype = str(font.get("Subtype") or "")
    if subtype == "Type0":
        return None, "Type0/Identity: codes are subset CIDs, no glyph names anywhere"
    if subtype not in SIMPLE_SUBTYPES:
        return None, "unsupported font subtype %r" % subtype

    enc = doc.resolve(font.get("Encoding"))
    base_name = None
    diffs = None
    if isinstance(enc, Name):
        base_name = str(enc)
    elif isinstance(enc, dict):
        b = doc.resolve(enc.get("BaseEncoding"))
        base_name = str(b) if isinstance(b, Name) else None
        d = doc.resolve(enc.get("Differences"))
        if isinstance(d, list):
            diffs = d
    elif enc is not None:
        return None, "unexpected /Encoding object"

    if base_name is None and diffs is None:
        # Ни базовой кодировки, ни списка отличий: остаётся встроенная
        # кодировка самого шрифта, а её мы не читаем. Догадка здесь хуже
        # отказа — неверная табличка испортит и то, что сейчас копируется.
        return None, "no /Encoding: built-in font encoding is not readable here"

    codes = dict(base_encoding_map(base_name or "StandardEncoding"))

    unresolved = []
    if diffs:
        cur = None
        for item in diffs:
            item = doc.resolve(item)
            if isinstance(item, Name):
                if cur is None or not (0 <= cur <= 255):
                    continue
                text = glyph_name_to_text(str(item))
                if text is None:
                    # .notdef -- не глиф, а его отсутствие; в списке
                    # нерешённых имён это шум.
                    if str(item) != ".notdef":
                        unresolved.append(str(item))
                    codes.pop(cur, None)  # базовая кодировка тут уже не власть
                else:
                    codes[cur] = text
                cur += 1
            elif isinstance(item, (int, float)):
                cur = int(item)

    # За пределами FirstChar..LastChar глифов у шрифта нет, и такие строки
    # в табличке только мешают читать её глазами.
    first = doc.resolve(font.get("FirstChar"))
    last = doc.resolve(font.get("LastChar"))
    if isinstance(first, (int, float)) and isinstance(last, (int, float)):
        lo, hi = int(first), int(last)
        if 0 <= lo <= hi <= 255:
            codes = dict((c, t) for c, t in codes.items() if lo <= c <= hi)

    codes = dict((c, t) for c, t in codes.items() if t and t != u"\u0000")
    if not codes:
        return None, "encoding yielded no usable characters"
    return codes, unresolved


# --------------------------------------------------------------------------- #
#  Сборка CMap
# --------------------------------------------------------------------------- #

CMAP_HEAD = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
"""

CMAP_TAIL = b"""endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def build_cmap(codes):
    """{код: строка} -> байты потока /ToUnicode."""
    out = [CMAP_HEAD]
    items = sorted(codes.items())
    # Ограничение формата: не больше 100 строк в одном блоке beginbfchar.
    for start in range(0, len(items), 100):
        chunk = items[start:start + 100]
        out.append(b"%d beginbfchar\n" % len(chunk))
        for code, text in chunk:
            hexed = text.encode("utf-16-be").hex().upper()
            out.append(b"<%02X> <%s>\n" % (code, hexed.encode("ascii")))
        out.append(b"endbfchar\n")
    out.append(CMAP_TAIL)
    return b"".join(out)


# --------------------------------------------------------------------------- #
#  Приращение файла
# --------------------------------------------------------------------------- #

_RE_OBJ_HEAD = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj")


def raw_object_body(doc, num):
    """Исходные байты объекта — без заголовка `N G obj` и без `endobj`.

    Словарь шрифта не пересобирается из разобранного дерева, а берётся
    байт в байт: пересборка неизбежно теряет мелочи (запись чисел,
    экранирование строк), а рисковать ради одной добавленной строки
    незачем.
    """
    entry = doc.xref.get(num)
    if entry is None:
        return None
    if entry[0] == "offset":
        m = _RE_OBJ_HEAD.match(doc.data, entry[1])
        if not m:
            return None
        end = doc.data.find(b"endobj", m.end())
        if end == -1:
            return None
        return doc.data[m.end():end]
    # Объект лежит в сжатом object stream. Приращение вынет его оттуда
    # и положит обычным объектом — так можно, ссылка в новой таблице
    # перекроет старую запись.
    stmnum, index = entry[1], entry[2]
    doc._get_from_objstm(stmnum, index)          # прогрев кэша
    cached = doc._objstm_cache.get(stmnum)
    if cached is None:
        return None
    data, first, pairs = cached
    if index >= len(pairs):
        return None
    off = first + pairs[index][1]
    starts = [first + o for _n, o in pairs]
    later = [s for s in starts if s > off]
    end = min(later) if later else len(data)
    return data[off:end]


def insert_tounicode(body, ref_num):
    """Вставляет /ToUnicode сразу после открывающей `<<` словаря.

    Перед `<<` не должно быть ничего, кроме пробелов: если там что-то есть,
    это не словарь шрифта, и вставка попала бы внутрь чужой скобки. Тихо
    испортить объект хуже, чем отказаться его чинить.
    """
    i = body.find(b"<<")
    if i == -1 or body[:i].strip():
        return None
    return body[:i + 2] + (b" /ToUnicode %d 0 R" % ref_num) + body[i + 2:]


def _pdf_string_hex(b):
    return b"<" + b.hex().upper().encode("ascii") + b">"


def _serialize_trailer_ref(v):
    if isinstance(v, Ref):
        return b"%d %d R" % (v.num, v.gen)
    return None


def xref_is_classic(doc, offset):
    """Какой формой записана секция ссылок по смещению: таблицей или потоком.

    Форму приращения обязан диктовать файл. Поток ссылок, дописанный поверх
    классической таблицы, CoreGraphics отвергает целиком: «missing or invalid
    cross-reference stream», и дальше читает файл только восстановлением
    по объектам. Проверено на Y123_215 — заголовок у файла %PDF-1.7, но
    таблица классическая, и по одной версии PDF форму не угадать.
    """
    m = re.compile(rb"\s*").match(doc.data, offset)
    p = offset + (m.end() - m.start() if m else 0)
    return doc.data[p:p + 4] == b"xref"


def _trailer_fields(doc, size, prev):
    out = [b"/Size %d" % size, b"/Prev %d" % prev]
    root = _serialize_trailer_ref(doc.trailer.get("Root"))
    if root:
        out.append(b"/Root " + root)
    info = _serialize_trailer_ref(doc.trailer.get("Info"))
    if info:
        out.append(b"/Info " + info)
    ids = doc.trailer.get("ID")
    if isinstance(ids, list) and len(ids) == 2 and all(isinstance(x, bytes) for x in ids):
        out.append(b"/ID [" + _pdf_string_hex(ids[0]) + b" " + _pdf_string_hex(ids[1]) + b"]")
    return out


def _consecutive_groups(nums):
    groups = []
    for n in nums:
        if groups and n == groups[-1][-1] + 1:
            groups[-1].append(n)
        else:
            groups.append([n])
    return groups


def _classic_section(doc, offsets, size, prev):
    """Классическая таблица ссылок для секции приращения.

    Строка записи обязана быть ровно двадцати байт — иначе читатель,
    который ходит по таблице арифметикой, а не разбором, съезжает.
    """
    out = bytearray(b"xref\r\n")
    out += b"0 1\r\n0000000000 65535 f\r\n"
    for group in _consecutive_groups(sorted(offsets)):
        out += b"%d %d\r\n" % (group[0], len(group))
        for n in group:
            out += b"%010d %05d n\r\n" % (offsets[n], 0)
    out += b"trailer\r\n<< " + b" ".join(_trailer_fields(doc, size, prev)) + b" >>\r\n"
    return bytes(out)


def _xref_stream_section(doc, offsets, size, prev, xref_num):
    nums = sorted(offsets)
    rows = bytearray()
    for n in nums:
        rows += b"\x01" + offsets[n].to_bytes(4, "big") + (0).to_bytes(2, "big")
    packed = zlib.compress(bytes(rows), 9)
    index = b" ".join(b"%d 1" % n for n in nums)
    d = [b"<< /Type /XRef /W [1 4 2]", b"/Index [%s]" % index]
    d.extend(_trailer_fields(doc, size, prev))
    d.append(b"/Filter /FlateDecode /Length %d >>" % len(packed))
    out = b"%d 0 obj\n" % xref_num + b"\n".join(d) + b"\nstream\n"
    return out + packed + b"\nendstream\nendobj\n"


def build_increment(doc, fixes, prev_startxref):
    """fixes: [(font_num, cmap_bytes)] -> байты, дописываемые в конец файла.

    Пишется полноценная секция приращения: новые потоки /ToUnicode,
    переписанные словари шрифтов и новая секция ссылок — той же формы,
    что уже в файле (см. xref_is_classic).
    """
    base = doc.data
    out = bytearray()
    # Приращение обязано начинаться с новой строки, иначе `1 0 obj` прилипнет
    # к хвосту предыдущего `%%EOF`.
    if not base.endswith(b"\n") and not base.endswith(b"\r"):
        out += b"\n"

    next_num = max(max(doc.xref) if doc.xref else 0,
                   int(doc.trailer.get("Size") or 0) - 1) + 1
    offsets = {}          # номер объекта -> смещение в готовом файле
    origin = len(base)

    for font_num, cmap in fixes:
        cmap_num = next_num
        next_num += 1

        packed = zlib.compress(cmap, 9)
        offsets[cmap_num] = origin + len(out)
        out += b"%d 0 obj\n<< /Filter /FlateDecode /Length %d >>\nstream\n" % (
            cmap_num, len(packed))
        out += packed
        out += b"\nendstream\nendobj\n"

        body = raw_object_body(doc, font_num)
        if body is None:
            raise PDFParseError("cannot read raw bytes of font object %d" % font_num)
        patched = insert_tounicode(body, cmap_num)
        if patched is None:
            raise PDFParseError("font object %d is not a dictionary" % font_num)
        offsets[font_num] = origin + len(out)
        out += b"%d 0 obj" % font_num + patched + b"endobj\n"

    if xref_is_classic(doc, prev_startxref):
        startxref = origin + len(out)
        out += _classic_section(doc, offsets, next_num, prev_startxref)
    else:
        xref_num = next_num
        next_num += 1
        startxref = origin + len(out)
        offsets[xref_num] = startxref
        out += _xref_stream_section(doc, offsets, next_num, prev_startxref, xref_num)

    out += b"startxref\n%d\n%%%%EOF\n" % startxref
    return bytes(out)


# --------------------------------------------------------------------------- #
#  Обработка одного файла
# --------------------------------------------------------------------------- #

def plan_file(path):
    """-> dict с разбором: что чиним, что не чиним, почему."""
    result = {"path": path, "error": None, "fixes": [], "skipped": [],
              "already": 0, "unresolved": []}
    try:
        doc = PDFDocument(str(path))
    except Exception as exc:
        result["error"] = "cannot parse: %s" % exc
        return result, None

    if doc.trailer.get("Encrypt") is not None:
        result["error"] = "encrypted PDF: strings would have to be encrypted too"
        return result, None

    for num, font in iter_font_objects(doc):
        name = str(font.get("BaseFont") or "?")
        if "ToUnicode" in font:
            result["already"] += 1
            continue
        codes, why = font_code_map(doc, font)
        if codes is None:
            result["skipped"].append((num, name, why))
            continue
        result["fixes"].append((num, name, codes))
        for n in why:
            if n not in result["unresolved"]:
                result["unresolved"].append(n)
    return result, doc


def apply_fixes(doc, result, out_path):
    fixes = [(num, build_cmap(codes)) for num, _name, codes in result["fixes"]]
    prev = doc._find_startxref()
    tail = build_increment(doc, fixes, prev)
    out_path.write_bytes(doc.data + tail)


def verify(out_path, expect_nums):
    """Перечитывает записанный файл и проверяет, что табличка встала.

    Проверка именно перечитыванием, а не доверием к своему же коду:
    приращение ломается тихо — старый файл читается как был, и разницы
    без сверки не видно.
    """
    doc = PDFDocument(str(out_path))
    bad = []
    for num in expect_nums:
        font = doc.get_object(num)
        if not isinstance(font, dict) or "ToUnicode" not in font:
            bad.append((num, "no /ToUnicode after write"))
            continue
        stm = doc.resolve(font["ToUnicode"])
        if not isinstance(stm, Stream):
            bad.append((num, "/ToUnicode is not a stream"))
            continue
        data = decode_stream(stm.dict, stm.raw, doc)
        if not data or b"beginbfchar" not in data:
            bad.append((num, "/ToUnicode stream is unreadable"))
    return bad


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="fix_tounicode.py",
        description="Add missing /ToUnicode CMaps to fonts in exported PDFs, "
                    "so that text can be copied out of Acrobat. The file is "
                    "changed by incremental update: original bytes are kept.")
    p.add_argument("paths", nargs="+", metavar="PATH",
                   help="PDF file or folder with PDFs (recursive)")
    p.add_argument("--dry-run", action="store_true",
                   help="report only, write nothing")
    p.add_argument("--in-place", action="store_true",
                   help="overwrite the source file instead of writing a copy")
    p.add_argument("--suffix", default="_uni", metavar="TEXT",
                   help="suffix for the written copy (default: _uni)")
    p.add_argument("--verbose", action="store_true",
                   help="list every font and every unresolved glyph name")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    paths = iter_pdf_paths(args.paths)
    if not paths:
        print("no PDF files found", file=sys.stderr)
        return 2

    n_files = n_fonts = n_codes = 0
    n_skipped = 0
    failures = []

    for path in paths:
        path = Path(path)
        result, doc = plan_file(path)
        if result["error"]:
            print("%s: %s" % (path.name, result["error"]))
            failures.append(path)
            continue
        if not result["fixes"] and not result["skipped"]:
            if args.verbose:
                print("%s: all %d fonts already have /ToUnicode"
                      % (path.name, result["already"]))
            continue

        codes_total = sum(len(c) for _n, _b, c in result["fixes"])
        print("%s: %d font(s) to fix, %d code(s); %d already fine"
              % (path.name, len(result["fixes"]), codes_total, result["already"]))
        if args.verbose:
            for num, name, codes in result["fixes"]:
                print("    fix  obj %-5d %-34s %d codes" % (num, name, len(codes)))
        for num, name, why in result["skipped"]:
            print("    skip obj %-5d %-34s %s" % (num, name, why))
            n_skipped += 1
        if result["unresolved"]:
            shown = result["unresolved"] if args.verbose else result["unresolved"][:8]
            print("    unmapped glyph names (%d): %s%s"
                  % (len(result["unresolved"]), " ".join(shown),
                     "" if args.verbose or len(result["unresolved"]) <= 8 else " ..."))

        if not result["fixes"] or args.dry_run:
            continue

        out_path = path if args.in_place else path.with_name(
            path.stem + args.suffix + path.suffix)
        try:
            apply_fixes(doc, result, out_path)
        except Exception as exc:
            print("    FAILED to write: %s" % exc)
            failures.append(path)
            continue
        bad = verify(out_path, [num for num, _n, _c in result["fixes"]])
        if bad:
            print("    FAILED verification: %s"
                  % "; ".join("obj %d %s" % b for b in bad))
            failures.append(path)
            continue
        print("    written: %s (+%d bytes)"
              % (out_path.name, out_path.stat().st_size - len(doc.data)))
        n_files += 1
        n_fonts += len(result["fixes"])
        n_codes += codes_total

    print("")
    if args.dry_run:
        print("dry run: nothing written")
    else:
        print("fixed %d file(s), %d font(s), %d code(s)" % (n_files, n_fonts, n_codes))
    if n_skipped:
        print("%d font(s) could not be fixed (see 'skip' lines above); "
              "those need a re-export from InDesign" % n_skipped)
    if failures:
        print("failed: %s" % ", ".join(p.name for p in failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
