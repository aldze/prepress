#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка готовых печатных PDF: RGB вместо CMYK, плашечные краски,
разрешение растровых картинок ниже порога.

    check_pdf.py путь/к/файлу.pdf
    check_pdf.py путь/к/папке               # все *.pdf внутри, рекурсивно
    check_pdf.py путь --min-ppi 300
    check_pdf.py путь --report отчёт.md

Разбирает структуру PDF самостоятельно (xref-таблицы и xref-потоки,
object streams, FlateDecode) — сторонних пакетов нет, только стдлиб,
как и везде в проекте (см. README, установка). Внешние
приложения (InDesign/Illustrator/Photoshop) не привлекаются: PDF —
уже готовый результат, а не связь, которую можно переоткрыть в Adobe.

Три проверки:
  - RGB вместо CMYK (DeviceRGB/CalRGB/ICCBased с 3 каналами) — в заливках,
    обводках, изображениях, шаблонах заливки и градиентах;
  - плашечные краски (Separation/DeviceN, кроме служебных All/None);
  - эффективное разрешение растровых картинок ниже порога (по умолчанию —
    upscale_below_ppi из config.json, тот же порог, что использует
    prepress.py для решения об апскейле).

Эффективный ppi считается по матрице трансформации в точке вставки
картинки (оператор Do) — так же, как effectivePpiH/V в prepress.py:
длина трансформированных рёбер единичного квадрата даёт физический
размер на странице в пунктах, оттуда и делится ширина/высота в пикселях.
Разбор идёт вглубь Form XObject и шаблонов заливки (tiling pattern);
внутри шаблонов заливки картинки проверяются на цвет, но не на
разрешение — матрица шаблона задаёт пространство относительно всей
страницы, а не относительно места конкретной вставки, и наивный подсчёт
там будет врать.

Не поддерживается: зашифрованные PDF (файл помечается как непроверенный,
без попытки расшифровки) и потоки с фильтрами, отличными от FlateDecode
(ASCIIHex/ASCII85 — есть; DCT/CCITT/JPX не нужны — из них читаются только
словари Width/Height/ColorSpace, не сами пиксели). Такие файлы и такие
страницы отмечаются в отчёте отдельно, а не проходят молча.
"""

import argparse
import datetime as _dt
import json
import math
import re
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
# Запасной порог на случай, если config.json потерялся или в нём нет ключа —
# 290 совпадает с upscale_below_ppi по умолчанию (см. config.json).
FALLBACK_MIN_PPI = 290.0


def configured_min_ppi():
    """Порог effective PPI из config.json (upscale_below_ppi) — тот же, что
    использует prepress.py для решения об апскейле."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        v = cfg.get("upscale_below_ppi")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    except (OSError, ValueError):
        pass
    return FALLBACK_MIN_PPI


# --------------------------------------------------------------------------- #
#  Модель объектов PDF
# --------------------------------------------------------------------------- #

class PDFParseError(Exception):
    pass


class Name(str):
    """Имя PDF (/Foo) — отдельный тип, чтобы не путать со строками в скобках."""
    pass


class Ref(object):
    """Косвенная ссылка "N G R"."""
    __slots__ = ("num", "gen")

    def __init__(self, num, gen):
        self.num = num
        self.gen = gen

    def __eq__(self, other):
        return isinstance(other, Ref) and self.num == other.num and self.gen == other.gen

    def __hash__(self):
        return hash((self.num, self.gen))

    def __repr__(self):
        return "Ref(%d,%d)" % (self.num, self.gen)


class Stream(object):
    __slots__ = ("dict", "raw")

    def __init__(self, d, raw):
        self.dict = d
        self.raw = raw


_WS = b" \t\r\n\f\x00"
_DELIMS = b"()<>[]{}/%"


def _is_hex_digit(b):
    return (0x30 <= b <= 0x39) or (0x41 <= b <= 0x46) or (0x61 <= b <= 0x66)


_ESCAPE_MAP = {0x6e: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12, 0x28: 0x28, 0x29: 0x29, 0x5c: 0x5c}


class Lexer(object):
    """Токенизатор синтаксиса PDF — общий для тела косвенных объектов
    (там, где нужны ссылки N G R) и для потоков содержимого страниц."""

    __slots__ = ("data", "pos", "n", "_pb")

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos
        self.n = len(data)
        self._pb = []

    def push_back(self, tok):
        self._pb.append(tok)

    def next_token(self):
        if self._pb:
            return self._pb.pop()
        data, n = self.data, self.n
        i = self.pos
        while True:
            if i >= n:
                self.pos = i
                return None
            c = data[i]
            if c in _WS:
                i += 1
                continue
            if c == 0x25:  # '%' — комментарий до конца строки
                j = data.find(b"\n", i)
                i = (j + 1) if j != -1 else n
                continue
            break
        c = data[i]
        if c == 0x2f:  # '/' имя
            j = i + 1
            out = []
            while j < n and data[j] not in _WS and data[j] not in _DELIMS:
                if data[j] == 0x23 and j + 2 < n and _is_hex_digit(data[j + 1]) and _is_hex_digit(data[j + 2]):
                    out.append(chr(int(data[j + 1:j + 3], 16)))
                    j += 3
                else:
                    out.append(chr(data[j]))
                    j += 1
            self.pos = j
            return ("NAME", "".join(out))
        if c == 0x28:  # '(' строка в скобках
            depth = 1
            j = i + 1
            out = bytearray()
            while j < n and depth > 0:
                ch = data[j]
                if ch == 0x5c:  # обратный слэш
                    if j + 1 >= n:
                        j += 1
                        break
                    nc = data[j + 1]
                    if nc in _ESCAPE_MAP:
                        out.append(_ESCAPE_MAP[nc])
                        j += 2
                    elif nc in (0x0d, 0x0a):
                        j += 2
                        if nc == 0x0d and j < n and data[j] == 0x0a:
                            j += 1
                    elif 0x30 <= nc <= 0x37:
                        k = j + 1
                        val = 0
                        cnt = 0
                        while k < n and cnt < 3 and 0x30 <= data[k] <= 0x37:
                            val = val * 8 + (data[k] - 0x30)
                            k += 1
                            cnt += 1
                        out.append(val & 0xFF)
                        j = k
                    else:
                        out.append(nc)
                        j += 2
                elif ch == 0x28:
                    depth += 1
                    out.append(ch)
                    j += 1
                elif ch == 0x29:
                    depth -= 1
                    if depth > 0:
                        out.append(ch)
                    j += 1
                else:
                    out.append(ch)
                    j += 1
            self.pos = j
            return ("STR", bytes(out))
        if c == 0x3c:
            if i + 1 < n and data[i + 1] == 0x3c:
                self.pos = i + 2
                return ("DICT_OPEN", None)
            j = data.find(b">", i + 1)
            if j == -1:
                j = n
            hexpart = re.sub(rb"\s+", b"", data[i + 1:j])
            if len(hexpart) % 2:
                hexpart += b"0"
            try:
                val = bytes.fromhex(hexpart.decode("ascii", "ignore"))
            except ValueError:
                val = b""
            self.pos = j + 1
            return ("STR", val)
        if c == 0x3e:
            if i + 1 < n and data[i + 1] == 0x3e:
                self.pos = i + 2
                return ("DICT_CLOSE", None)
            self.pos = i + 1
            return self.next_token()
        if c == 0x5b:
            self.pos = i + 1
            return ("ARR_OPEN", None)
        if c == 0x5d:
            self.pos = i + 1
            return ("ARR_CLOSE", None)
        if c in b"+-.0123456789":
            j = i + 1
            while j < n and data[j] in b"+-.0123456789Ee":
                j += 1
            text = data[i:j]
            self.pos = j
            try:
                if b"." in text or b"e" in text or b"E" in text:
                    return ("NUM", float(text))
                return ("NUM", int(text))
            except ValueError:
                try:
                    return ("NUM", float(text))
                except ValueError:
                    return ("NUM", 0)
        # оператор/ключевое слово — всё, что не распознано выше
        j = i
        while j < n and data[j] not in _WS and data[j] not in _DELIMS:
            j += 1
        if j == i:
            j = i + 1
        self.pos = j
        return ("KW", data[i:j].decode("latin-1"))


def parse_value(lex, tok):
    """Строит значение PDF (число/имя/строка/массив/словарь/ссылка) начиная
    с уже прочитанного токена. Общий и для тел объектов (нужны ссылки
    N G R), и для операндов в потоке содержимого (там ссылок не бывает,
    поэтому лишний просмотр вперёд им не мешает)."""
    kind, val = tok
    if kind == "NUM":
        t2 = lex.next_token()
        if t2 is not None and t2[0] == "NUM":
            t3 = lex.next_token()
            if t3 is not None and t3[0] == "KW" and t3[1] == "R":
                return Ref(int(val), int(t2[1]))
            if t3 is not None:
                lex.push_back(t3)
            lex.push_back(t2)
            return val
        if t2 is not None:
            lex.push_back(t2)
        return val
    if kind == "NAME":
        return Name(val)
    if kind == "STR":
        return val
    if kind == "ARR_OPEN":
        items = []
        while True:
            t = lex.next_token()
            if t is None or t[0] == "ARR_CLOSE":
                break
            items.append(parse_value(lex, t))
        return items
    if kind == "DICT_OPEN":
        d = {}
        while True:
            t = lex.next_token()
            if t is None or t[0] == "DICT_CLOSE":
                break
            if t[0] != "NAME":
                continue
            key = str(t[1])
            t2 = lex.next_token()
            if t2 is None:
                break
            d[key] = parse_value(lex, t2)
        return d
    if kind == "KW":
        if val == "true":
            return True
        if val == "false":
            return False
        if val == "null":
            return None
        return Name(val)
    return None


# --------------------------------------------------------------------------- #
#  Фильтры потоков
# --------------------------------------------------------------------------- #

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def apply_predictor(data, params, doc):
    predictor = params.get("Predictor", 1)
    predictor = int(doc.resolve(predictor) if doc else (predictor or 1)) if predictor is not None else 1
    if predictor <= 1:
        return data
    colors = params.get("Colors", 1)
    colors = int(doc.resolve(colors) if doc else (colors or 1)) if colors is not None else 1
    bpc = params.get("BitsPerComponent", 8)
    bpc = int(doc.resolve(bpc) if doc else (bpc or 8)) if bpc is not None else 8
    columns = params.get("Columns", 1)
    columns = int(doc.resolve(columns) if doc else (columns or 1)) if columns is not None else 1
    bpp = max(1, (colors * bpc) // 8)
    rowlen = (colors * bpc * columns + 7) // 8
    if rowlen <= 0:
        return data
    if predictor == 2:
        out = bytearray(data)
        if bpc == 8:
            for r0 in range(0, len(out) - rowlen + 1, rowlen):
                for j in range(bpp, rowlen):
                    out[r0 + j] = (out[r0 + j] + out[r0 + j - bpp]) & 0xFF
        return bytes(out)
    # PNG-предикторы (>= 10): каждая строка начинается с байта типа фильтра
    out = bytearray()
    prev = bytearray(rowlen)
    i = 0
    n = len(data)
    while i + 1 + rowlen <= n:
        ftype = data[i]
        row = bytearray(data[i + 1:i + 1 + rowlen])
        i += 1 + rowlen
        if ftype == 1:
            for j in range(rowlen):
                left = row[j - bpp] if j >= bpp else 0
                row[j] = (row[j] + left) & 0xFF
        elif ftype == 2:
            for j in range(rowlen):
                row[j] = (row[j] + prev[j]) & 0xFF
        elif ftype == 3:
            for j in range(rowlen):
                left = row[j - bpp] if j >= bpp else 0
                row[j] = (row[j] + ((left + prev[j]) // 2)) & 0xFF
        elif ftype == 4:
            for j in range(rowlen):
                left = row[j - bpp] if j >= bpp else 0
                up = prev[j]
                upleft = prev[j - bpp] if j >= bpp else 0
                row[j] = (row[j] + _paeth(left, up, upleft)) & 0xFF
        out.extend(row)
        prev = row
    return bytes(out)


def _ascii85_decode(data):
    data = data.split(b"~>")[0]
    data = re.sub(rb"\s+", b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    out = bytearray()
    group = []
    try:
        i = 0
        n = len(data)
        while i < n:
            c = data[i]
            if c == 0x7a and not group:  # 'z' — четыре нулевых байта
                out.extend(b"\x00\x00\x00\x00")
                i += 1
                continue
            group.append(c - 33)
            i += 1
            if len(group) == 5:
                val = 0
                for g in group:
                    val = val * 85 + g
                out.extend(val.to_bytes(4, "big"))
                group = []
        if group:
            pad = 5 - len(group)
            group.extend([84] * pad)
            val = 0
            for g in group:
                val = val * 85 + g
            chunk = val.to_bytes(4, "big")
            out.extend(chunk[:4 - pad])
    except (ValueError, OverflowError):
        return None
    return bytes(out)


def decode_stream(d, raw, doc):
    """None означает «не смогли раскодировать» — вызывающий код должен
    отступиться от этого потока с предупреждением, а не падать."""
    filters = d.get("Filter")
    if filters is None:
        return raw
    filters = doc.resolve(filters) if doc else filters
    if isinstance(filters, Name):
        filters = [filters]
    elif not isinstance(filters, list):
        return None
    params = d.get("DecodeParms")
    if params is None:
        params = d.get("DP")
    params = doc.resolve(params) if doc else params
    if params is None:
        params = [{}] * len(filters)
    elif isinstance(params, dict):
        params = [params]
    elif isinstance(params, list):
        params = [(doc.resolve(x) if doc else x) for x in params]
    else:
        params = [{}] * len(filters)
    while len(params) < len(filters):
        params.append({})
    out = raw
    for f, p in zip(filters, params):
        fname = str(doc.resolve(f) if doc else f)
        p = p if isinstance(p, dict) else {}
        if fname in ("FlateDecode", "Fl"):
            try:
                out = zlib.decompress(out)
            except zlib.error:
                try:
                    dobj = zlib.decompressobj()
                    out = dobj.decompress(out) + dobj.flush()
                except zlib.error:
                    return None
            out = apply_predictor(out, p, doc)
        elif fname in ("ASCIIHexDecode", "AHx"):
            body = out.split(b">")[0]
            body = re.sub(rb"\s+", b"", body)
            if len(body) % 2:
                body += b"0"
            try:
                out = bytes.fromhex(body.decode("ascii", "ignore"))
            except ValueError:
                return None
        elif fname in ("ASCII85Decode", "A85"):
            out = _ascii85_decode(out)
            if out is None:
                return None
        else:
            return None
    return out


def decode_stream_safe(doc, stream_obj):
    try:
        return decode_stream(stream_obj.dict, stream_obj.raw, doc)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Документ: xref (классический и потоковый), object streams, объекты
# --------------------------------------------------------------------------- #

class PDFDocument(object):
    def __init__(self, path):
        self.data = Path(path).read_bytes()
        self.xref = {}
        self.trailer = {}
        self.cache = {}
        self._objstm_cache = {}
        self._load_xref()

    # ---- разбор одного косвенного объекта по смещению ---- #

    def _parse_indirect_at(self, p):
        lex = Lexer(self.data, p)
        t1 = lex.next_token()
        t2 = lex.next_token()
        t3 = lex.next_token()
        if not (t1 and t1[0] == "NUM" and t2 and t2[0] == "NUM"
                and t3 and t3[0] == "KW" and t3[1] == "obj"):
            raise PDFParseError("ожидался заголовок объекта по смещению %d" % p)
        objnum = int(t1[1])
        tok = lex.next_token()
        if tok is None:
            raise PDFParseError("пустой объект %d" % objnum)
        value = parse_value(lex, tok)
        nxt = lex.next_token()
        if nxt is not None and nxt[0] == "KW" and nxt[1] == "stream":
            data = self.data
            i = lex.pos
            if i < len(data) and data[i:i + 1] == b"\r":
                i += 1
            if i < len(data) and data[i:i + 1] == b"\n":
                i += 1
            length = value.get("Length") if isinstance(value, dict) else None
            if isinstance(length, Ref):
                length = self.resolve(length)
            end = None
            if isinstance(length, (int, float)) and length >= 0:
                cand = i + int(length)
                tail = data[cand:cand + 20]
                if re.match(rb"\s*endstream", tail):
                    end = cand
            if end is None:
                end = self._scan_endstream(data, i)
            raw = data[i:end]
            return objnum, Stream(value if isinstance(value, dict) else {}, raw)
        return objnum, value

    @staticmethod
    def _scan_endstream(data, i):
        j = data.find(b"endstream", i)
        if j == -1:
            return len(data)
        k = j
        while k > i and data[k - 1] in b"\r\n":
            k -= 1
        return k

    # ---- xref ---- #

    def _find_startxref(self):
        tail_len = min(2048, len(self.data))
        tail = self.data[-tail_len:]
        idx = tail.rfind(b"startxref")
        if idx == -1:
            raise PDFParseError("не найден startxref")
        base = len(self.data) - tail_len
        lex = Lexer(self.data, base + idx + len(b"startxref"))
        tok = lex.next_token()
        if tok is None or tok[0] != "NUM":
            raise PDFParseError("некорректный startxref")
        return int(tok[1])

    def _load_xref(self):
        try:
            start = self._find_startxref()
        except PDFParseError:
            start = None
        seen = set()
        queue = [start] if start is not None else []
        while queue:
            pos = queue.pop(0)
            if pos is None or pos in seen or pos < 0 or pos >= len(self.data):
                continue
            seen.add(pos)
            m = re.match(rb"\s*", self.data[pos:pos + 16])
            p = pos + (m.end() if m else 0)
            try:
                if self.data[p:p + 4] == b"xref":
                    entries, trailer, prev, xrefstm = self._parse_classic_xref(p)
                else:
                    entries, trailer, prev, xrefstm = self._parse_xref_stream_at(p)
            except PDFParseError:
                continue
            for objnum, kind_, a, b in entries:
                if objnum in self.xref:
                    continue
                if kind_ == 0:
                    continue
                elif kind_ == 1:
                    self.xref[objnum] = ("offset", a)
                elif kind_ == 2:
                    self.xref[objnum] = ("objstm", a, b)
            for k, v in trailer.items():
                if k not in self.trailer:
                    self.trailer[k] = v
            if xrefstm is not None:
                queue.append(xrefstm)
            if prev is not None:
                queue.append(prev)
        if not self.trailer.get("Root"):
            self._recover_by_scanning()

    def _parse_classic_xref(self, p):
        lex = Lexer(self.data, p)
        lex.next_token()  # 'xref'
        entries = []
        while True:
            save = lex.pos
            tok = lex.next_token()
            if tok is None:
                break
            if tok[0] == "KW" and tok[1] == "trailer":
                break
            if tok[0] != "NUM":
                lex.pos = save
                break
            start_num = int(tok[1])
            tok2 = lex.next_token()
            if tok2 is None or tok2[0] != "NUM":
                break
            count = int(tok2[1])
            for k in range(count):
                t1 = lex.next_token()
                t2 = lex.next_token()
                t3 = lex.next_token()
                if t1 is None or t2 is None or t3 is None:
                    break
                if t3[0] == "KW" and t3[1] == "n" and t1[0] == "NUM":
                    entries.append((start_num + k, 1, int(t1[1]), int(t2[1])))
        trailer = {}
        prev = None
        xrefstm = None
        tok = lex.next_token()
        if tok is not None and tok[0] == "DICT_OPEN":
            trailer = parse_value(lex, tok)
            if isinstance(trailer, dict):
                pv = trailer.get("Prev")
                if isinstance(pv, (int, float)):
                    prev = int(pv)
                xv = trailer.get("XRefStm")
                if isinstance(xv, (int, float)):
                    xrefstm = int(xv)
        return entries, (trailer if isinstance(trailer, dict) else {}), prev, xrefstm

    def _parse_xref_stream_at(self, p):
        objnum, obj = self._parse_indirect_at(p)
        if not isinstance(obj, Stream):
            raise PDFParseError("ожидался поток перекрёстных ссылок по смещению %d" % p)
        d = obj.dict
        data = decode_stream(d, obj.raw, self)
        if data is None:
            raise PDFParseError("не удалось раскодировать поток перекрёстных ссылок")
        W = d.get("W") or [1, 1, 1]
        W = [int(x) for x in W]
        size = int(d.get("Size") or 0)
        index = d.get("Index")
        index = [int(x) for x in index] if isinstance(index, list) else [0, size]
        rec_len = sum(W)
        entries = []
        pos = 0
        it = iter(index)
        for start_num, count in zip(it, it):
            for k in range(count):
                chunk = data[pos:pos + rec_len]
                pos += rec_len
                if len(chunk) < rec_len:
                    break
                vals = []
                off = 0
                for wi, w in enumerate(W):
                    if w == 0:
                        vals.append(1 if wi == 0 else 0)
                    else:
                        vals.append(int.from_bytes(chunk[off:off + w], "big"))
                        off += w
                entries.append((start_num + k, vals[0], vals[1], vals[2] if len(vals) > 2 else 0))
        prev = d.get("Prev")
        prev = int(prev) if isinstance(prev, (int, float)) else None
        return entries, d, prev, None

    def _recover_by_scanning(self):
        """xref повреждён или отсутствует — ищем все "N G obj" по файлу и
        берём /Root из любого /Type /Catalog, а не только из трейлера."""
        for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", self.data):
            objnum = int(m.group(1))
            self.xref.setdefault(objnum, ("offset", m.start()))
        if not self.trailer.get("Root"):
            for m in re.finditer(rb"trailer\b", self.data):
                lex = Lexer(self.data, m.end())
                tok = lex.next_token()
                if tok and tok[0] == "DICT_OPEN":
                    d = parse_value(lex, tok)
                    if isinstance(d, dict) and d.get("Root") is not None and not self.trailer.get("Root"):
                        self.trailer["Root"] = d["Root"]
        if not self.trailer.get("Root"):
            for num in list(self.xref.keys()):
                try:
                    obj = self.get_object(num)
                except Exception:
                    continue
                if isinstance(obj, dict) and str(obj.get("Type") or "") == "Catalog":
                    self.trailer["Root"] = Ref(num, 0)
                    break
        if not self.trailer.get("Root"):
            raise PDFParseError("не найден /Root даже при восстановлении по объектам")

    # ---- object streams (ObjStm) ---- #

    def _get_from_objstm(self, stream_objnum, index):
        cached = self._objstm_cache.get(stream_objnum)
        if cached is None:
            stm = self.get_object(stream_objnum)
            if not isinstance(stm, Stream):
                return None
            data = decode_stream(stm.dict, stm.raw, self)
            if data is None:
                return None
            n = stm.dict.get("N")
            n = int(self.resolve(n) or 0)
            first = stm.dict.get("First")
            first = int(self.resolve(first) or 0)
            lex = Lexer(data, 0)
            pairs = []
            for _ in range(n):
                t1 = lex.next_token()
                t2 = lex.next_token()
                if t1 is None or t2 is None or t1[0] != "NUM" or t2[0] != "NUM":
                    break
                pairs.append((int(t1[1]), int(t2[1])))
            cached = (data, first, pairs)
            self._objstm_cache[stream_objnum] = cached
        data, first, pairs = cached
        if index < 0 or index >= len(pairs):
            return None
        _onum, off = pairs[index]
        lex2 = Lexer(data, first + off)
        tok = lex2.next_token()
        if tok is None:
            return None
        return parse_value(lex2, tok)

    # ---- доступ к объектам ---- #

    def get_object(self, num):
        if num in self.cache:
            return self.cache[num]
        entry = self.xref.get(num)
        if entry is None:
            return None
        self.cache[num] = None  # защита от зацикливания при повторном обращении
        try:
            if entry[0] == "offset":
                _n, val = self._parse_indirect_at(entry[1])
            else:
                val = self._get_from_objstm(entry[1], entry[2])
        except Exception:
            val = None
        self.cache[num] = val
        return val

    def resolve(self, v):
        seen = 0
        while isinstance(v, Ref):
            v = self.get_object(v.num)
            seen += 1
            if seen > 64:
                return None
        return v


# --------------------------------------------------------------------------- #
#  Цветовые пространства
# --------------------------------------------------------------------------- #

# Служебные имена красителя в Separation/DeviceN, которые не считаются
# плашкой: All/None — по спецификации PDF (8.6.6.4), Cyan/Magenta/Yellow/
# Black — стандартный приём Adobe-приложений (InDesign, Illustrator,
# Distiller) для объекта на одном процессном канале (типично — чёрный текст
# через отдельный K, без registration/rich black); Acrobat Preflight по той
# же причине не считает их плашками.
_NON_SPOT_COLORANTS = frozenset(("All", "None", "Cyan", "Magenta", "Yellow", "Black"))


def classify_colorspace(doc, cs, resources, depth=0):
    """-> (kind, spot_names). kind: gray/rgb/cmyk/lab/spot/pattern/other."""
    if depth > 12:
        return ("other", [])
    cs = doc.resolve(cs)
    if isinstance(cs, Name):
        s = str(cs)
        if s == "DeviceGray":
            return ("gray", [])
        if s == "DeviceRGB":
            return ("rgb", [])
        if s == "DeviceCMYK":
            return ("cmyk", [])
        if s == "Pattern":
            return ("pattern", [])
        cs_res = doc.resolve((resources or {}).get("ColorSpace"))
        if isinstance(cs_res, dict) and s in cs_res:
            return classify_colorspace(doc, cs_res[s], resources, depth + 1)
        return ("other", [])
    if isinstance(cs, list) and cs:
        head = doc.resolve(cs[0])
        head = str(head) if head is not None else ""
        if head == "ICCBased":
            stream = doc.resolve(cs[1]) if len(cs) > 1 else None
            n = None
            alt = None
            if isinstance(stream, Stream):
                n = doc.resolve(stream.dict.get("N"))
                alt = stream.dict.get("Alternate")
            if n == 1:
                return ("gray", [])
            if n == 3:
                return ("rgb", [])
            if n == 4:
                return ("cmyk", [])
            if alt is not None:
                return classify_colorspace(doc, alt, resources, depth + 1)
            return ("other", [])
        if head == "CalRGB":
            return ("rgb", [])
        if head == "CalGray":
            return ("gray", [])
        if head == "Lab":
            return ("lab", [])
        if head == "Indexed":
            base = cs[1] if len(cs) > 1 else None
            return classify_colorspace(doc, base, resources, depth + 1)
        if head == "Separation":
            names = doc.resolve(cs[1]) if len(cs) > 1 else None
            spot_names = []
            if isinstance(names, Name) and str(names) not in _NON_SPOT_COLORANTS:
                spot_names = [str(names)]
            if len(cs) > 2:
                alt_kind, _ = classify_colorspace(doc, cs[2], resources, depth + 1)
            else:
                alt_kind = "cmyk"
            return ("spot", spot_names) if spot_names else (alt_kind, [])
        if head == "DeviceN":
            names = doc.resolve(cs[1]) if len(cs) > 1 else []
            spot_names = []
            if isinstance(names, list):
                for nm in names:
                    nm = doc.resolve(nm)
                    s2 = str(nm) if nm is not None else ""
                    if s2 and s2 not in _NON_SPOT_COLORANTS:
                        spot_names.append(s2)
            return ("spot", spot_names) if spot_names else ("other", [])
        if head == "Pattern":
            return ("pattern", [])
    return ("other", [])


# --------------------------------------------------------------------------- #
#  Разбор содержимого страниц
# --------------------------------------------------------------------------- #

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mult(m1, m2):
    """M1 применяется первым, затем M2 — как в PDF (cm домножает CTM слева)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def transform_point(ctm, x, y):
    a, b, c, d, e, f = ctm
    return (a * x + c * y + e, b * x + d * y + f)


_MM_PER_PT = 25.4 / 72.0


def bbox_mm_from_points(points, mediabox):
    """points — точки в пространстве страницы (уже под CTM). mediabox —
    (x0, y0, x1, y1) в pt или None, если для страницы не нашли. Возвращает
    (отступ_слева_мм, отступ_сверху_мм, ширина_мм, высота_мм) или None."""
    if not points or mediabox is None:
        return None
    x0, y0, x1, y1 = mediabox
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return (
        (min_x - x0) * _MM_PER_PT,
        (y1 - max_y) * _MM_PER_PT,
        (max_x - min_x) * _MM_PER_PT,
        (max_y - min_y) * _MM_PER_PT,
    )


def bbox_mm_from_ctm_unit_square(ctm, mediabox):
    corners = [transform_point(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
    return bbox_mm_from_points(corners, mediabox)


def _no_negative_zero(v):
    # round() может вернуть -0.0 у объектов впритык к краю страницы —
    # "%.0f" тогда печатает "-0", хотя по смыслу это ровно 0.
    v = round(v)
    return 0.0 if v == 0 else v


def format_position(bbox_mm):
    if bbox_mm is None:
        return "—"
    left, top, w, h = bbox_mm
    return "%.0f×%.0f мм, отступ слева %.0f сверху %.0f" % (
        w, h, _no_negative_zero(left), _no_negative_zero(top))


class GState(object):
    # fill_issue/stroke_issue — (kind, spots, op) с последнего rg/RG/scn/SCN,
    # если цвет проблемный (rgb/spot), иначе None. Замечание печатается не
    # в момент выбора цвета, а в момент реальной закраски/обводки контура —
    # тогда уже есть его геометрия для положения на странице.
    __slots__ = ("ctm", "fill_cs", "stroke_cs", "fill_issue", "stroke_issue")

    def __init__(self, ctm, fill_cs, stroke_cs):
        self.ctm = ctm
        self.fill_cs = fill_cs
        self.stroke_cs = stroke_cs
        self.fill_issue = None
        self.stroke_issue = None

    def copy(self):
        g = GState(self.ctm, self.fill_cs, self.stroke_cs)
        g.fill_issue = self.fill_issue
        g.stroke_issue = self.stroke_issue
        return g


class Checker(object):
    def __init__(self):
        self.color_hits = []  # (page, kind, spot_names_tuple, context, op, bbox_mm)
        self.image_hits = []  # (page, name, w, h, ppi, bbox_mm)
        self.warnings = []    # (page, message)
        self.mediabox = None  # (x0, y0, x1, y1) в pt текущей страницы, либо None

    def hit_color(self, page, kind, spots, context, op, bbox_mm=None):
        self.color_hits.append((page, kind, tuple(spots), context, op, bbox_mm))

    def hit_image(self, page, name, w, h, ppi, bbox_mm=None):
        self.image_hits.append((page, name, w, h, ppi, bbox_mm))

    def warn(self, page, msg):
        self.warnings.append((page, msg))


INLINE_KEY_MAP = {
    "BPC": "BitsPerComponent", "CS": "ColorSpace", "D": "Decode", "DP": "DecodeParms",
    "F": "Filter", "H": "Height", "IM": "ImageMask", "I": "Interpolate",
    "W": "Width", "L": "Length",
}
INLINE_CS_MAP = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK", "I": "Indexed"}


def _find_inline_image_end(data, start):
    i = start
    n = len(data)
    while True:
        idx = data.find(b"EI", i)
        if idx == -1:
            return n
        before_ok = idx > start and data[idx - 1] in b" \t\r\n\x00\f"
        nxt = data[idx + 2:idx + 3]
        after_ok = (nxt == b"" or nxt in (b" ", b"\t", b"\r", b"\n", b"/", b"[", b"Q", b"q", b"<"))
        if before_ok and after_ok:
            return idx - 1
        i = idx + 2
    return n


def _image_ppi(w, h, ctm):
    a, b, c, d, _e, _f = ctm
    len_x = math.hypot(a, b)
    len_y = math.hypot(c, d)
    if len_x <= 1e-6 or len_y <= 1e-6:
        return None
    ppi_x = w / (len_x / 72.0)
    ppi_y = h / (len_y / 72.0)
    return min(ppi_x, ppi_y)


def handle_inline_image(doc, lex, resources, ctm, page_no, checker):
    d = {}
    while True:
        t = lex.next_token()
        if t is None:
            return
        if t[0] == "KW" and t[1] == "ID":
            break
        if t[0] != "NAME":
            continue
        key = INLINE_KEY_MAP.get(t[1], t[1])
        t2 = lex.next_token()
        if t2 is None:
            return
        d[key] = parse_value(lex, t2)
    data = lex.data
    start = lex.pos
    if start < len(data) and data[start:start + 1] in (b" ", b"\t", b"\r", b"\n"):
        start += 1
    end = _find_inline_image_end(data, start)
    lex.pos = end
    lex.next_token()  # 'EI'
    if d.get("ImageMask"):
        return
    cs = d.get("ColorSpace")
    if isinstance(cs, Name):
        cs = Name(INLINE_CS_MAP.get(str(cs), str(cs)))
    if cs is not None:
        kind_, spots = classify_colorspace(doc, cs, resources)
        if kind_ in ("rgb", "spot"):
            bbox_mm = bbox_mm_from_ctm_unit_square(ctm, checker.mediabox)
            checker.hit_color(page_no, kind_, spots, "встроенное изображение", "BI/ColorSpace", bbox_mm)
    w, h = d.get("Width"), d.get("Height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
        ppi = _image_ppi(w, h, ctm)
        if ppi is not None:
            bbox_mm = bbox_mm_from_ctm_unit_square(ctm, checker.mediabox)
            checker.hit_image(page_no, "встроенное изображение", int(w), int(h), ppi, bbox_mm)


def process_image(doc, xobj, ctm, page_no, checker, name, in_pattern, resources):
    d = xobj.dict
    if doc.resolve(d.get("ImageMask")):
        return
    cs = d.get("ColorSpace")
    if cs is not None:
        kind_, spots = classify_colorspace(doc, cs, resources)
        if kind_ in ("rgb", "spot"):
            bbox_mm = bbox_mm_from_ctm_unit_square(ctm, checker.mediabox)
            checker.hit_color(page_no, kind_, spots, "изображение %s" % name, "ColorSpace", bbox_mm)
    if in_pattern:
        return
    w = doc.resolve(d.get("Width"))
    h = doc.resolve(d.get("Height"))
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
        ppi = _image_ppi(w, h, ctm)
        if ppi is not None:
            bbox_mm = bbox_mm_from_ctm_unit_square(ctm, checker.mediabox)
            checker.hit_image(page_no, name, int(w), int(h), ppi, bbox_mm)


def classify_and_report_shading(doc, shading, resources, page_no, checker, context):
    if isinstance(shading, Stream):
        sd = shading.dict
    elif isinstance(shading, dict):
        sd = shading
    else:
        return
    cs = sd.get("ColorSpace")
    if cs is None:
        return
    kind_, spots = classify_colorspace(doc, cs, resources)
    if kind_ in ("rgb", "spot"):
        # Геометрия градиента/шаблона-заливки не восстанавливается так же просто,
        # как у изображения (нет единичного квадрата) — положение не показываем.
        checker.hit_color(page_no, kind_, spots, context, "Shading/ColorSpace", None)


def handle_shading(doc, resources, name, page_no, checker):
    shs = doc.resolve((resources or {}).get("Shading")) if resources else None
    if not isinstance(shs, dict) or name not in shs:
        return
    classify_and_report_shading(doc, doc.resolve(shs[name]), resources, page_no, checker,
                                 "заливка градиентом %s" % name)


def handle_pattern(doc, resources, name, ctm, page_no, checker, depth, in_pattern, visited):
    pats = doc.resolve((resources or {}).get("Pattern")) if resources else None
    if not isinstance(pats, dict) or name not in pats:
        return
    pat = doc.resolve(pats[name])
    pdict = pat.dict if isinstance(pat, Stream) else (pat if isinstance(pat, dict) else None)
    if not isinstance(pdict, dict):
        return
    ptype = doc.resolve(pdict.get("PatternType"))
    if ptype == 2:
        shading = doc.resolve(pdict.get("Shading"))
        classify_and_report_shading(doc, shading, resources, page_no, checker,
                                     "шаблон-заливка %s" % name)
    elif ptype == 1 and isinstance(pat, Stream) and depth < 24:
        pm = pdict.get("Matrix")
        matrix = tuple(float(doc.resolve(v)) for v in pm) if isinstance(pm, list) and len(pm) == 6 else IDENTITY
        pres = doc.resolve(pdict.get("Resources")) or resources
        data = decode_stream_safe(doc, pat)
        if data is not None:
            # Матрица шаблона задаёт пространство относительно страницы, а не
            # относительно текущего CTM в месте scn — брать её как есть, не
            # домножая на текущий ctm, чтобы не исказить эффективный ppi
            # изображений внутри шаблона (их и так не считаем — in_pattern=True).
            run_content(doc, data, pres, matrix, page_no, checker, depth + 1, True, visited)


def handle_xobject(doc, resources, name, ctm, page_no, checker, depth, in_pattern, visited):
    xobjs = doc.resolve((resources or {}).get("XObject")) if resources else None
    if not isinstance(xobjs, dict) or name not in xobjs:
        return
    ref = xobjs[name]
    xobj = doc.resolve(ref)
    if not isinstance(xobj, Stream):
        return
    subtype = str(doc.resolve(xobj.dict.get("Subtype")) or "")
    if subtype == "Image":
        process_image(doc, xobj, ctm, page_no, checker, name, in_pattern, resources)
        return
    if subtype != "Form":
        return
    cycle_key = (ref.num, ref.gen) if isinstance(ref, Ref) else id(xobj)
    if cycle_key in visited:
        checker.warn(page_no, "циклическая ссылка Form XObject %s — пропущена" % name)
        return
    if depth >= 24:
        checker.warn(page_no, "слишком глубокая вложенность Form XObject — часть страницы пропущена")
        return
    visited = visited | {cycle_key}
    fm = xobj.dict.get("Matrix")
    matrix = tuple(float(doc.resolve(v)) for v in fm) if isinstance(fm, list) and len(fm) == 6 else IDENTITY
    new_ctm = mat_mult(matrix, ctm)
    fres = doc.resolve(xobj.dict.get("Resources")) or resources
    data = decode_stream_safe(doc, xobj)
    if data is None:
        checker.warn(page_no, "форма %s: неподдерживаемый фильтр потока — пропущена" % name)
        return
    run_content(doc, data, fres, new_ctm, page_no, checker, depth + 1, in_pattern, visited)


# Операторы построения контура (операнды — координаты в текущем пространстве,
# трансформируются CTM в момент вызова) и операторы закраски/обводки, которые
# его завершают. 'n' тоже завершает контур, но ничего не красит (клиппинг).
_PATH_FILL_OPS = frozenset(("f", "F", "f*", "B", "B*", "b", "b*"))
_PATH_STROKE_OPS = frozenset(("S", "s", "B", "B*", "b", "b*"))
_PATH_END_OPS = _PATH_FILL_OPS | _PATH_STROKE_OPS | frozenset(("n",))


def run_content(doc, data, resources, ctm, page_no, checker, depth=0, in_pattern=False, visited=None):
    if depth > 24:
        checker.warn(page_no, "слишком глубокая вложенность содержимого — часть страницы пропущена")
        return
    if visited is None:
        visited = frozenset()
    lex = Lexer(data)
    stack = []
    gstack = []
    gs = GState(ctm, Name("DeviceGray"), Name("DeviceGray"))
    path_pts = []  # точки текущего контура в пространстве страницы (уже под CTM)
    while True:
        try:
            tok = lex.next_token()
        except Exception:
            checker.warn(page_no, "ошибка разбора содержимого — часть страницы пропущена")
            break
        if tok is None:
            break
        kind, val = tok
        if kind == "NUM" or kind == "STR":
            stack.append(val)
            continue
        if kind == "NAME":
            stack.append(Name(val))
            continue
        if kind == "ARR_OPEN" or kind == "DICT_OPEN":
            try:
                stack.append(parse_value(lex, tok))
            except Exception:
                pass
            continue
        if kind != "KW":
            continue
        op = val
        if op == "true":
            stack.append(True)
            continue
        if op == "false":
            stack.append(False)
            continue
        if op == "null":
            stack.append(None)
            continue
        if op == "BI":
            try:
                handle_inline_image(doc, lex, resources, gs.ctm, page_no, checker)
            except Exception:
                checker.warn(page_no, "не удалось разобрать встроенное изображение")
            stack = []
            continue
        try:
            if op == "q":
                gstack.append(gs.copy())
            elif op == "Q":
                if gstack:
                    gs = gstack.pop()
            elif op == "cm" and len(stack) >= 6:
                m = tuple(float(x) for x in stack[-6:])
                gs.ctm = mat_mult(m, gs.ctm)
            elif op in ("m", "l") and len(stack) >= 2:
                path_pts.append(transform_point(gs.ctm, float(stack[-2]), float(stack[-1])))
            elif op == "c" and len(stack) >= 6:
                x1, y1, x2, y2, x3, y3 = (float(x) for x in stack[-6:])
                # Кубическая кривая целиком лежит в выпуклой оболочке контрольных
                # точек — для bbox этого достаточно, не решаем саму кривую.
                for px, py in ((x1, y1), (x2, y2), (x3, y3)):
                    path_pts.append(transform_point(gs.ctm, px, py))
            elif op in ("v", "y") and len(stack) >= 4:
                x2, y2, x3, y3 = (float(x) for x in stack[-4:])
                for px, py in ((x2, y2), (x3, y3)):
                    path_pts.append(transform_point(gs.ctm, px, py))
            elif op == "re" and len(stack) >= 4:
                x, y, w, h = (float(v) for v in stack[-4:])
                for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
                    path_pts.append(transform_point(gs.ctm, px, py))
            elif op in ("cs", "CS") and stack and isinstance(stack[-1], Name):
                if op == "cs":
                    gs.fill_cs = stack[-1]
                    gs.fill_issue = None
                else:
                    gs.stroke_cs = stack[-1]
                    gs.stroke_issue = None
            elif op in ("rg", "RG"):
                if op == "rg":
                    gs.fill_issue = ("rgb", (), op)
                else:
                    gs.stroke_issue = ("rgb", (), op)
            elif op in ("k", "K", "g", "G"):
                if op in ("k", "g"):
                    gs.fill_issue = None
                else:
                    gs.stroke_issue = None
            elif op in ("sc", "scn", "SC", "SCN"):
                cs = gs.fill_cs if op in ("sc", "scn") else gs.stroke_cs
                pattern_name = stack[-1] if stack and isinstance(stack[-1], Name) else None
                kind_, spots = classify_colorspace(doc, cs, resources)
                issue = (kind_, spots, op) if kind_ in ("rgb", "spot") else None
                if op in ("sc", "scn"):
                    gs.fill_issue = issue
                else:
                    gs.stroke_issue = issue
                if kind_ == "pattern" and pattern_name is not None:
                    handle_pattern(doc, resources, str(pattern_name), gs.ctm, page_no, checker,
                                    depth, in_pattern, visited)
            elif op == "sh" and stack and isinstance(stack[-1], Name):
                handle_shading(doc, resources, str(stack[-1]), page_no, checker)
            elif op == "Do" and stack and isinstance(stack[-1], Name):
                handle_xobject(doc, resources, str(stack[-1]), gs.ctm, page_no, checker,
                                depth, in_pattern, visited)
            if op in _PATH_END_OPS:
                bbox_mm = bbox_mm_from_points(path_pts, checker.mediabox)
                if op in _PATH_FILL_OPS and gs.fill_issue is not None:
                    kind_, spots, srcop = gs.fill_issue
                    checker.hit_color(page_no, kind_, spots, "заливка", srcop, bbox_mm)
                if op in _PATH_STROKE_OPS and gs.stroke_issue is not None:
                    kind_, spots, srcop = gs.stroke_issue
                    checker.hit_color(page_no, kind_, spots, "обводка", srcop, bbox_mm)
                path_pts = []
        except Exception:
            checker.warn(page_no, "ошибка обработки оператора %s — пропущен" % op)
        stack = []


# --------------------------------------------------------------------------- #
#  Дерево страниц и проверка одного файла
# --------------------------------------------------------------------------- #

def collect_pages(doc):
    root = doc.resolve(doc.trailer.get("Root"))
    if not isinstance(root, dict):
        raise PDFParseError("не найден корневой каталог документа (/Root)")
    pages_root = root.get("Pages")
    if pages_root is None:
        raise PDFParseError("не найдено дерево страниц (/Pages)")
    pages = []
    visited = set()

    def walk(node_ref, inherited, depth):
        if depth > 64:
            return
        node = doc.resolve(node_ref)
        if not isinstance(node, dict):
            return
        if id(node) in visited:
            return
        visited.add(id(node))
        res = dict(inherited)
        if "Resources" in node:
            res["Resources"] = node["Resources"]
        if "MediaBox" in node:
            res["MediaBox"] = node["MediaBox"]
        kids = node.get("Kids")
        if kids is not None:
            kids = doc.resolve(kids)
            if isinstance(kids, list):
                for kid in kids:
                    walk(kid, res, depth + 1)
        else:
            pages.append((node, res))

    walk(pages_root, {}, 0)
    return pages


def _resolve_mediabox(doc, mb):
    mb = doc.resolve(mb)
    if not isinstance(mb, list) or len(mb) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(doc.resolve(v)) for v in mb)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def check_pdf_file(path, min_ppi):
    result = {"path": str(path), "ok": False, "pages": 0, "color_hits": [],
              "image_hits": [], "warnings": [], "error": None}
    try:
        doc = PDFDocument(path)
    except Exception as e:
        result["error"] = "не удалось прочитать структуру файла: %s" % e
        return result
    if doc.trailer.get("Encrypt") is not None:
        result["error"] = "файл защищён (шифрование PDF) — проверка не поддерживается"
        return result
    try:
        pages = collect_pages(doc)
    except Exception as e:
        result["error"] = "не удалось разобрать дерево страниц: %s" % e
        return result
    checker = Checker()
    for idx, (node, res) in enumerate(pages, start=1):
        resources = doc.resolve(res.get("Resources")) or {}
        if not isinstance(resources, dict):
            resources = {}
        checker.mediabox = _resolve_mediabox(doc, res.get("MediaBox"))
        contents = doc.resolve(node.get("Contents"))
        streams = contents if isinstance(contents, list) else ([contents] if contents is not None else [])
        raw_parts = []
        for s in streams:
            s = doc.resolve(s)
            if isinstance(s, Stream):
                data = decode_stream_safe(doc, s)
                if data is None:
                    checker.warn(idx, "неподдерживаемый фильтр в потоке содержимого — часть страницы пропущена")
                    continue
                raw_parts.append(data)
        content_bytes = b"\n".join(raw_parts)
        try:
            run_content(doc, content_bytes, resources, IDENTITY, idx, checker)
        except Exception:
            checker.warn(idx, "не удалось разобрать содержимое страницы")
    result["ok"] = True
    result["pages"] = len(pages)
    result["color_hits"] = checker.color_hits
    result["image_hits"] = [h for h in checker.image_hits if h[4] < min_ppi]
    result["warnings"] = checker.warnings
    return result


# --------------------------------------------------------------------------- #
#  Обход путей и отчёт
# --------------------------------------------------------------------------- #

def iter_pdf_paths(inputs):
    paths = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_dir():
            paths.extend(sorted(p.rglob("*.pdf")))
        elif p.is_file():
            paths.append(p)
        else:
            print("предупреждение: путь не найден — %s" % raw, file=sys.stderr)
    seen = set()
    uniq = []
    for p in paths:
        rp = str(p.resolve())
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def write_report(path, results, min_ppi, started_at, elapsed):
    L = []
    A = L.append
    A("# Проверка печатных PDF")
    A("")
    A("- **Дата:** %s (%.1f с)" % (started_at, elapsed))
    A("- **Порог разрешения:** %.0f ppi (`upscale_below_ppi` из config.json)" % min_ppi)
    A("- **Проверено файлов:** %d" % len(results))
    A("")

    ok_files = [r for r in results if r["error"] is None and not r["color_hits"] and not r["image_hits"]]
    bad_files = [r for r in results if r["error"] is None and (r["color_hits"] or r["image_hits"])]
    err_files = [r for r in results if r["error"] is not None]

    A("| Показатель | Значение |")
    A("|---|---|")
    A("| Без замечаний | %d |" % len(ok_files))
    A("| С замечаниями | %d |" % len(bad_files))
    A("| Не удалось проверить | %d |" % len(err_files))
    A("")

    if bad_files:
        A("## ⚠ Файлы с замечаниями")
        A("")
        for r in bad_files:
            A("### `%s`" % r["path"])
            A("")
            A("- Страниц: %d" % r["pages"])
            rgb = [h for h in r["color_hits"] if h[1] == "rgb"]
            spot = [h for h in r["color_hits"] if h[1] == "spot"]
            if rgb:
                A("")
                A("**RGB вместо CMYK:**")
                A("")
                A("| Стр. | Где | Положение на странице |")
                A("|---|---|---|")
                seen = set()
                for page, _kind, _spots, context, _op, bbox_mm in sorted(rgb, key=lambda x: (x[0], x[3])):
                    key = (page, context, bbox_mm)
                    if key in seen:
                        continue
                    seen.add(key)
                    A("| %d | %s | %s |" % (page, context, format_position(bbox_mm)))
            if spot:
                A("")
                A("**Плашечные краски:**")
                A("")
                A("| Стр. | Плашка | Где | Положение на странице |")
                A("|---|---|---|---|")
                seen = set()
                for page, _kind, spots, context, _op, bbox_mm in sorted(spot, key=lambda x: (x[0], x[3])):
                    key = (page, spots, context, bbox_mm)
                    if key in seen:
                        continue
                    seen.add(key)
                    A("| %d | %s | %s | %s |" % (page, ", ".join(spots) or "?", context, format_position(bbox_mm)))
            if r["image_hits"]:
                A("")
                A("**Разрешение ниже %.0f ppi:**" % min_ppi)
                A("")
                A("| Стр. | Пикселей | Effective PPI | Положение на странице |")
                A("|---|---|---|---|")
                for page, _name, w, h, ppi, bbox_mm in sorted(set(r["image_hits"]), key=lambda x: (x[0], -x[4])):
                    A("| %d | %d×%d | %.0f | %s |" % (page, w, h, ppi, format_position(bbox_mm)))
            if r["warnings"]:
                A("")
                uniq_warn = sorted(set("стр. %d: %s" % (p, m) for p, m in r["warnings"]))
                A("_Предупреждения разбора:_ " + "; ".join(uniq_warn))
            A("")

    if err_files:
        A("## Не удалось проверить")
        A("")
        A("| Файл | Причина |")
        A("|---|---|")
        for r in err_files:
            A("| `%s` | %s |" % (r["path"], r["error"]))
        A("")

    if ok_files:
        A("## Без замечаний")
        A("")
        for r in ok_files:
            warn_note = " — есть предупреждения разбора, см. лог" if r["warnings"] else ""
            A("- `%s` (%d стр.)%s" % (r["path"], r["pages"], warn_note))
        A("")

    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="check_pdf.py",
        description="Проверка готовых печатных PDF: RGB вместо CMYK, "
                     "плашечные краски, разрешение растровых картинок.")
    p.add_argument("paths", nargs="+", metavar="ПУТЬ",
                    help="PDF-файл или папка с PDF (рекурсивно)")
    p.add_argument("--min-ppi", type=float, default=None,
                    help="Порог разрешения, ppi (по умолчанию upscale_below_ppi "
                         "из config.json, обычно 290)")
    p.add_argument("--report", metavar="ФАЙЛ",
                    help="Путь к сводному .md отчёту "
                         "(по умолчанию рядом с первым проверяемым путём)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    min_ppi = args.min_ppi if args.min_ppi is not None else configured_min_ppi()

    pdf_paths = iter_pdf_paths(args.paths)
    if not pdf_paths:
        print("PDF-файлы не найдены.", file=sys.stderr)
        return 2

    started = _dt.datetime.now()
    results = []
    for p in pdf_paths:
        print("проверка: %s" % p)
        r = check_pdf_file(p, min_ppi)
        results.append(r)
        if r["error"]:
            print("  не удалось проверить: %s" % r["error"])
        else:
            n_issues = len(r["color_hits"]) + len(r["image_hits"])
            print("  %s" % ("замечаний нет" if n_issues == 0 else "замечаний: %d" % n_issues))
    elapsed = (_dt.datetime.now() - started).total_seconds()

    if args.report:
        report_path = Path(args.report).expanduser()
    else:
        first = Path(args.paths[0]).expanduser()
        base_dir = first if first.is_dir() else first.parent
        report_path = base_dir / ("pdf_check_%s.md" % started.strftime("%Y%m%d_%H%M%S"))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report_path, results, min_ppi, started.strftime("%Y-%m-%d %H:%M:%S"), elapsed)
    print("\nОтчёт: %s" % report_path)

    any_issues = any(r["error"] or r["color_hits"] or r["image_hits"] for r in results)
    return 1 if any_issues else 0


if __name__ == "__main__":
    sys.exit(main())
