#!/usr/bin/env python3
"""Minimal stdlib readers for Excel files -- legacy binary .xls (BIFF8/BIFF5
inside an OLE2 compound file) and .xlsx (zipped SpreadsheetML) -- enough for
the plain data exports county election offices post (ES&S "Absent Voter
Details", candidate-tool downloads and the like): cell text and numbers by
sheet. No formatting, no formulas beyond their cached results.

    sheets = read_xls(raw_bytes)          # [(sheet_name, rows)]
    sheets = read_xlsx(raw_bytes)
    sheets = read_any(raw_bytes)          # picks by the file's magic bytes
    rows -> list of lists (str | float | bool | None), ragged-right trimmed
    excel_date(serial) -> datetime.date
"""
import io
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

_END = 0xFFFFFFFE
_FREE = 0xFFFFFFFF


def _ole_streams(raw):
    """{stream name: bytes} for the top-level streams of an OLE2 file."""
    if raw[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("not an OLE2 (.xls) file")
    ssz = 1 << struct.unpack_from("<H", raw, 0x1E)[0]
    mssz = 1 << struct.unpack_from("<H", raw, 0x20)[0]
    n_fat, dir_start = struct.unpack_from("<II", raw, 0x2C)
    cutoff, minifat_start, n_minifat, dif_start, n_dif = struct.unpack_from("<IIIII", raw, 0x38)

    def sector(n):
        return raw[(n + 1) * ssz:(n + 2) * ssz]

    difat = list(struct.unpack_from("<109I", raw, 0x4C))
    s, per = dif_start, ssz // 4
    for _ in range(n_dif):
        if s in (_END, _FREE):
            break
        ent = struct.unpack("<%dI" % per, sector(s))
        difat.extend(ent[:-1])
        s = ent[-1]
    fat = []
    for s in difat[:n_fat]:
        fat.extend(struct.unpack("<%dI" % per, sector(s)))

    def chain(start, table, read, limit):
        out, s, seen = [], start, 0
        while s not in (_END, _FREE) and s < len(table) and seen < limit:
            out.append(read(s))
            s = table[s]
            seen += 1
        return b"".join(out)

    limit = len(raw) // ssz + 2
    dirs = chain(dir_start, fat, sector, limit)
    entries = []
    for i in range(0, len(dirs) - 127, 128):
        e = dirs[i:i + 128]
        nlen = struct.unpack_from("<H", e, 0x40)[0]
        name = e[:max(0, nlen - 2)].decode("utf-16-le", "replace")
        etype = e[0x42]
        start, size = struct.unpack_from("<II", e, 0x74)
        entries.append((name, etype, start, size))
    root = next((e for e in entries if e[1] == 5), None)
    ministream = chain(root[2], fat, sector, limit) if root else b""
    minifat = []
    if n_minifat:
        mf = chain(minifat_start, fat, sector, limit)
        minifat = list(struct.unpack("<%dI" % (len(mf) // 4), mf))

    def minisector(n):
        return ministream[n * mssz:(n + 1) * mssz]

    out = {}
    for name, etype, start, size in entries:
        if etype != 2:
            continue
        if size < cutoff:
            data = chain(start, minifat, minisector, len(ministream) // mssz + 2)
        else:
            data = chain(start, fat, sector, limit)
        out[name] = data[:size]
    return out


class _Chunks:
    """SST data spread over CONTINUE records; character runs that cross a
    record boundary restart with a fresh option byte."""

    def __init__(self, chunks):
        self.chunks, self.i, self.pos = chunks, 0, 0

    def _ensure(self):
        while self.i < len(self.chunks) and self.pos >= len(self.chunks[self.i]):
            self.i += 1
            self.pos = 0
        return self.i < len(self.chunks)

    def read(self, n):
        out = b""
        while n > 0 and self._ensure():
            c = self.chunks[self.i]
            take = c[self.pos:self.pos + n]
            out += take
            self.pos += len(take)
            n -= len(take)
        return out

    def chars(self, n, high):
        parts = []
        while n > 0:
            if self.i < len(self.chunks) and self.pos >= len(self.chunks[self.i]):
                if not self._ensure():
                    break
                high = self.chunks[self.i][self.pos] & 1
                self.pos += 1
            if self.i >= len(self.chunks):
                break
            c = self.chunks[self.i]
            width = 2 if high else 1
            avail = (len(c) - self.pos) // width
            k = min(n, avail)
            seg = c[self.pos:self.pos + k * width]
            parts.append(seg.decode("utf-16-le" if high else "latin-1", "replace"))
            self.pos += k * width
            n -= k
            if k == 0 and avail == 0:
                self.pos = len(c)
        return "".join(parts)


def _sst(chunks):
    rd = _Chunks(chunks)
    total, unique = struct.unpack("<II", rd.read(8))
    out = []
    for _ in range(unique):
        head = rd.read(3)
        if len(head) < 3:
            break
        cch, flags = struct.unpack("<HB", head)
        runs = struct.unpack("<H", rd.read(2))[0] if flags & 0x08 else 0
        ext = struct.unpack("<I", rd.read(4))[0] if flags & 0x04 else 0
        out.append(rd.chars(cch, flags & 0x01))
        if runs:
            rd.read(4 * runs)
        if ext:
            rd.read(ext)
    return out


def _rk(v):
    if v & 2:
        n = (v if v < 0x80000000 else v - 0x100000000) >> 2
    else:
        n = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return n / 100 if v & 1 else float(n)


def _ustr(data, off, biff8):
    """XLUnicodeString (BIFF8: cch u16, flags u8, chars; BIFF5: cch u16, bytes)."""
    cch = struct.unpack_from("<H", data, off)[0]
    if not biff8:
        return data[off + 2:off + 2 + cch].decode("latin-1", "replace")
    flags = data[off + 2]
    p = off + 3
    if flags & 0x08:
        p += 2
    if flags & 0x04:
        p += 4
    if flags & 0x01:
        return data[p:p + 2 * cch].decode("utf-16-le", "replace")
    return data[p:p + cch].decode("latin-1", "replace")


def _records(wb, start=0):
    pos = start
    while pos + 4 <= len(wb):
        rtype, rlen = struct.unpack_from("<HH", wb, pos)
        yield pos, rtype, wb[pos + 4:pos + 4 + rlen]
        pos += 4 + rlen


def read_xls(raw):
    streams = _ole_streams(raw)
    names = {k.lower(): v for k, v in streams.items()}
    wb = names.get("workbook") or names.get("book")
    if wb is None:
        raise ValueError("no Workbook stream")
    biff8, sst, sheets, pending = True, [], [], None
    for pos, rtype, data in _records(wb):
        if rtype == 0x0809 and pos == 0:
            biff8 = struct.unpack_from("<H", data, 0)[0] >= 0x0600
        elif rtype == 0x0085:          # BOUNDSHEET
            off = struct.unpack_from("<I", data, 0)[0]
            cch = data[6]              # short string: 1-byte length (+ BIFF8 flags byte)
            if biff8:
                wide = data[7] & 1
                name = data[8:8 + cch * (2 if wide else 1)].decode("utf-16-le" if wide else "latin-1", "replace")
            else:
                name = data[7:7 + cch].decode("latin-1", "replace")
            sheets.append((name, off, data[5]))
        elif rtype == 0x00FC:          # SST (+ CONTINUE)
            pending = [data]
        elif rtype == 0x003C and pending is not None:
            pending.append(data)
        else:
            if pending is not None:
                sst = _sst(pending)
                pending = None
            if rtype == 0x000A:        # EOF of the globals
                break
    out = []
    for name, off, kind in sheets:
        if kind not in (0, None):      # worksheets only
            continue
        cells = {}
        fstring = None
        for pos, rtype, data in _records(wb, off):
            if pos != off and rtype == 0x0809:
                break
            if rtype == 0x000A:
                break
            try:
                if rtype == 0x00FD:    # LABELSST
                    r, c, _, i = struct.unpack_from("<HHHI", data)
                    cells[(r, c)] = sst[i] if i < len(sst) else ""
                elif rtype == 0x0204:  # LABEL
                    r, c = struct.unpack_from("<HH", data)
                    cells[(r, c)] = _ustr(data, 6, biff8)
                elif rtype == 0x0203:  # NUMBER
                    r, c, _, v = struct.unpack_from("<HHHd", data)
                    cells[(r, c)] = v
                elif rtype == 0x027E:  # RK
                    r, c, _, v = struct.unpack_from("<HHHI", data)
                    cells[(r, c)] = _rk(v)
                elif rtype == 0x00BD:  # MULRK
                    r, c0 = struct.unpack_from("<HH", data)
                    n = (len(data) - 6) // 6
                    for k in range(n):
                        cells[(r, c0 + k)] = _rk(struct.unpack_from("<I", data, 4 + 6 * k + 2)[0])
                elif rtype == 0x0205:  # BOOLERR
                    r, c, _, v, is_err = struct.unpack_from("<HHHBB", data)
                    cells[(r, c)] = None if is_err else bool(v)
                elif rtype == 0x0006:  # FORMULA (cached result)
                    r, c = struct.unpack_from("<HH", data)
                    res = data[6:14]
                    if res[6:8] == b"\xff\xff":
                        if res[0] == 0:
                            fstring = (r, c)
                        elif res[0] == 1:
                            cells[(r, c)] = bool(res[2])
                    else:
                        cells[(r, c)] = struct.unpack("<d", res)[0]
                elif rtype == 0x0207 and fstring:  # STRING (formula text result)
                    cells[fstring] = _ustr(data, 0, biff8)
                    fstring = None
            except struct.error:
                continue
        if not cells:
            out.append((name, []))
            continue
        nrows = max(r for r, _ in cells) + 1
        rows = [[] for _ in range(nrows)]
        for (r, c), v in cells.items():
            row = rows[r]
            if len(row) <= c:
                row.extend([None] * (c + 1 - len(row)))
            row[c] = v
        out.append((name, rows))
    return out


def excel_date(serial):
    return date(1899, 12, 30) + timedelta(days=int(serial))


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _col_index(ref):
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def read_xlsx(raw):
    """Worksheets of an .xlsx in workbook order; namespace-prefix agnostic."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
            if _local(el.tag) == "si":
                shared.append("".join(t.text or "" for t in el.iter() if _local(t.tag) == "t"))
                el.clear()
    sheets = []
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels.iter() if _local(r.tag) == "Relationship"}
        for sh in wb.iter():
            if _local(sh.tag) == "sheet":
                rid = next((v for k, v in sh.attrib.items() if _local(k) == "id"), None)
                t = (target.get(rid) or "").lstrip("/")
                sheets.append((sh.get("name"), t if t.startswith("xl/") else "xl/" + t))
    except (KeyError, ET.ParseError):
        pass
    if not sheets:
        sheets = [(n.rsplit("/", 1)[-1], n) for n in sorted(names) if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    out = []
    for name, path in sheets:
        if path not in names:
            continue
        rows = []
        for _, el in ET.iterparse(z.open(path)):
            if _local(el.tag) != "row":
                continue
            r = int(el.get("r") or len(rows) + 1) - 1
            while len(rows) <= r:
                rows.append([])
            row = rows[r]
            for c in el:
                if _local(c.tag) != "c":
                    continue
                kind = c.get("t")
                v = next((x.text for x in c if _local(x.tag) == "v"), None)
                if kind == "s" and v is not None:
                    val = shared[int(v)]
                elif kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
                elif kind in ("str", "e"):
                    val = v
                elif kind == "b":
                    val = v == "1"
                elif v is not None:
                    try:
                        val = float(v)
                    except ValueError:
                        val = v
                else:
                    val = None
                ci = _col_index(c.get("r") or "") if c.get("r") else len(row)
                if len(row) <= ci:
                    row.extend([None] * (ci + 1 - len(row)))
                row[ci] = val
            el.clear()
        out.append((name, rows))
    return out


def read_any(raw):
    if raw[:2] == b"PK":
        return read_xlsx(raw)
    return read_xls(raw)
