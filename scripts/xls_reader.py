"""Minimal reader for legacy Excel .xls files (OLE2 compound file + BIFF8), stdlib only.

read_xls(data) -> {sheet_name: [[cell, ...], ...]} with cells as str / float / None.
Handles the record types plain data exports use: shared strings (SST + CONTINUE),
LABELSST, LABEL, NUMBER, RK, MULRK and cached FORMULA numbers. Formatting is ignored.
"""
import struct

ENDOFCHAIN = 0xFFFFFFFE


def _ole_stream(data, want="Workbook"):
    if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("not an OLE2 file")
    ssz = 1 << struct.unpack_from("<H", data, 0x1E)[0]
    nfat, dir1 = struct.unpack_from("<II", data, 0x2C)
    difat1, ndifat = struct.unpack_from("<II", data, 0x44)
    sec = lambda i: data[512 + i * ssz: 512 + (i + 1) * ssz]  # noqa: E731
    fat_secs = list(struct.unpack_from("<109I", data, 0x4C))
    d = difat1
    for _ in range(ndifat):
        if d >= ENDOFCHAIN:
            break
        vals = struct.unpack("<%dI" % (ssz // 4), sec(d))
        fat_secs += vals[:-1]
        d = vals[-1]
    fat = []
    for s in fat_secs[:nfat]:
        fat += struct.unpack("<%dI" % (ssz // 4), sec(s))

    def chain(start):
        out, s, seen = [], start, set()
        while s < ENDOFCHAIN and s not in seen:
            seen.add(s)
            out.append(sec(s))
            s = fat[s]
        return b"".join(out)
    dirs = chain(dir1)
    for i in range(0, len(dirs), 128):
        e = dirs[i:i + 128]
        nlen = struct.unpack_from("<H", e, 0x40)[0]
        name = e[:max(nlen - 2, 0)].decode("utf-16-le", "replace")
        if name in (want, "Book"):
            start, size = struct.unpack_from("<II", e, 0x74)
            if size < struct.unpack_from("<I", data, 0x38)[0]:
                raise ValueError("workbook stored in the mini stream (tiny file) - not supported")
            return chain(start)[:size]
    raise ValueError("no Workbook stream")


def _records(wb):
    p = 0
    while p + 4 <= len(wb):
        rt, ln = struct.unpack_from("<HH", wb, p)
        yield p, rt, wb[p + 4:p + 4 + ln]
        p += 4 + ln


def _rk(v):
    if v & 2:
        x = v >> 2
        if x & 0x20000000:
            x -= 0x40000000
        n = float(x)
    else:
        n = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return n / 100 if v & 1 else n


def _sst(chunks):
    """chunks: SST data followed by its CONTINUE records' data, kept separate."""
    strings = []
    ci, buf = 0, chunks[0]
    pos = 8
    total = struct.unpack_from("<I", buf, 4)[0]

    def need(n):
        nonlocal ci, buf, pos
        if pos + n <= len(buf):
            return
        if pos >= len(buf):
            ci += 1
            buf, pos = chunks[ci], 0

    for _ in range(total):
        need(3)
        cch, flags = struct.unpack_from("<HB", buf, pos)
        pos += 3
        runs = ext = 0
        if flags & 8:
            need(2)
            runs = struct.unpack_from("<H", buf, pos)[0]
            pos += 2
        if flags & 4:
            need(4)
            ext = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        wide = flags & 1
        parts, left = [], cch
        while left:
            if pos >= len(buf):
                ci += 1
                buf = chunks[ci]
                wide = buf[0] & 1
                pos = 1
            w = 2 if wide else 1
            take = min(left, (len(buf) - pos) // w)
            raw = buf[pos:pos + take * w]
            parts.append(raw.decode("utf-16-le" if wide else "latin-1"))
            pos += take * w
            left -= take
        skip = runs * 4 + ext
        while skip:
            if pos >= len(buf):
                ci += 1
                buf, pos = chunks[ci], 0
            t = min(skip, len(buf) - pos)
            pos += t
            skip -= t
        strings.append("".join(parts))
    return strings


def read_xls(data):
    wb = _ole_stream(data)
    recs = list(_records(wb))
    sheets, sst = [], []
    for i, (p, rt, d) in enumerate(recs):
        if rt == 0x85:
            pos = struct.unpack_from("<I", d, 0)[0]
            ln, fl = d[6], d[7]
            name = d[8:8 + ln * (2 if fl & 1 else 1)].decode("utf-16-le" if fl & 1 else "latin-1")
            sheets.append((pos, name))
        elif rt == 0xFC:
            chunks = [d]
            j = i + 1
            while j < len(recs) and recs[j][1] == 0x3C:
                chunks.append(recs[j][2])
                j += 1
            sst = _sst(chunks)
    by_pos = {pos: name for pos, name in sheets}
    out, cur, grid = {}, None, None

    def put(r, c, v):
        while len(grid) <= r:
            grid.append([])
        row = grid[r]
        while len(row) <= c:
            row.append(None)
        row[c] = v
    for p, rt, d in recs:
        if rt == 0x809 and p in by_pos:
            cur, grid = by_pos[p], []
            out[cur] = grid
        elif cur is None:
            continue
        elif rt == 0xFD:
            r, c, _, k = struct.unpack_from("<HHHI", d)
            put(r, c, sst[k])
        elif rt == 0x203:
            r, c, _, v = struct.unpack_from("<HHHd", d)
            put(r, c, v)
        elif rt == 0x27E:
            r, c, _, v = struct.unpack_from("<HHHI", d)
            put(r, c, _rk(v))
        elif rt == 0xBD:
            r, c = struct.unpack_from("<HH", d)
            n = (len(d) - 6) // 6
            for k in range(n):
                v = struct.unpack_from("<I", d, 4 + k * 6 + 2)[0]
                put(r, c + k, _rk(v))
        elif rt == 0x204:
            r, c, _, ln = struct.unpack_from("<HHHH", d)
            fl = d[8]
            put(r, c, d[9:9 + ln * (2 if fl & 1 else 1)].decode("utf-16-le" if fl & 1 else "latin-1"))
        elif rt == 0x06:
            r, c, _ = struct.unpack_from("<HHH", d)
            if d[12:14] != b"\xff\xff":
                put(r, c, struct.unpack_from("<d", d, 6)[0])
        elif rt == 0x0A:
            cur = None
    return out
