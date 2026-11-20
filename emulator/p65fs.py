"""P65 flash file system, mailbox executor and USB protocol server in pure Python.

This is a line-by-line port of the firmware's C code (src/fs.c, src/storage_core.c,
src/usb_proto.c) and produces **the same bytes on the flash image** and the same results.
tests/test_py_vs_c.py runs both implementations side by side and compares them, so whenever
the C code changes, that test shows what to change here (and vice versa).

Return values follow the C code: FS_* codes (0 = OK, negative = error), STO_* result codes for
the mailbox, status bytes for the USB protocol.
"""

import zlib

# ---- sizes -----------------------------------------------------------------------------------
FS_SECTOR_SIZE = 4096
FS_PAGE_SIZE = 256
FS_MAX_SECTORS = 512
FS_MAX_FILES = 256
FS_NAME_LEN = 11
FS_FIRST_DATA = FS_SECTOR_SIZE - 64
FS_CONT_DATA = FS_SECTOR_SIZE - 32

FS_OK, FS_ERR_IO, FS_ERR_NOTFOUND, FS_ERR_FULL, FS_ERR_BADNAME = 0, -1, -2, -3, -4
FS_ERR_CORRUPT, FS_ERR_ARG, FS_ERR_EXISTS = -5, -6, -7

# ---- on-flash layout (must equal src/fs.c) -----------------------------------------------------
HDR_MAGIC = 0x32534650                       # "PFS2"
K_FIRST, K_CONT = 1, 2
H_KIND, H_FLAGS, H_SEQ, H_NEXT, H_USED, H_GEN, H_ECOUNT, H_DCRC, H_HCRC = 4, 5, 6, 8, 10, 12, 16, 20, 28
M_NAME, M_TYPE, M_LOAD, M_EXEC, M_SIZE, M_CRC = 32, 43, 44, 46, 48, 52
NONE16 = 0xFFFF
BLANK_PAGE = b'\xff' * FS_PAGE_SIZE


def crc32(data, crc=0):
    return zlib.crc32(bytes(data), crc) & 0xFFFFFFFF


def _u16(b, o):
    return b[o] | (b[o + 1] << 8)


def _u32(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)


def _put16(b, o, v):
    b[o] = v & 0xFF
    b[o + 1] = (v >> 8) & 0xFF


def _put32(b, o, v):
    for i in range(4):
        b[o + i] = (v >> (8 * i)) & 0xFF


class PowerCut(Exception):
    """Raised by Flash when a simulated power failure hits."""


class Flash:
    """NOR flash in memory: erase sets a sector to 0xFF, program can only clear bits.
    Counts the operations (for the emulator's timing model) and can simulate a power cut."""

    def __init__(self, size, image=None):
        self.size = size
        self.img = bytearray(b'\xff' * size)
        if image:
            n = min(len(image), size)
            self.img[:n] = image[:n]
        self.erases = 0
        self.programs = 0
        self.dirty = False
        self.cut_at = None            # operation number at which power fails
        self.cut_mode = 'before'      # before / half / after
        self.ops = 0

    def read(self, off, n):
        if off < 0 or off + n > self.size:
            raise IOError('read outside flash')
        return bytes(self.img[off:off + n])

    def _tick(self):
        self.ops += 1
        return self.cut_at is not None and self.ops == self.cut_at

    def erase(self, off):
        if off % FS_SECTOR_SIZE or off + FS_SECTOR_SIZE > self.size:
            return -1
        cut = self._tick()
        if cut:
            if self.cut_mode == 'after':
                self.img[off:off + FS_SECTOR_SIZE] = b'\xff' * FS_SECTOR_SIZE
            elif self.cut_mode == 'half':
                self.img[off:off + FS_SECTOR_SIZE // 2] = b'\xff' * (FS_SECTOR_SIZE // 2)
            raise PowerCut()
        self.img[off:off + FS_SECTOR_SIZE] = b'\xff' * FS_SECTOR_SIZE
        self.erases += 1
        self.dirty = True
        return 0

    def program(self, off, data):
        n = len(data)
        if off % FS_PAGE_SIZE or n % FS_PAGE_SIZE or off + n > self.size:
            return -1
        cut = self._tick()
        if cut:
            if self.cut_mode == 'after':
                self._and(off, data)
            elif self.cut_mode == 'half':
                self._and(off, data[:n // 2])
            raise PowerCut()
        self._and(off, data)
        self.programs += n // FS_PAGE_SIZE
        self.dirty = True
        return 0

    def _and(self, off, data):
        cur = self.img[off:off + len(data)]
        self.img[off:off + len(data)] = bytes(a & b for a, b in zip(cur, data))


class Entry:
    __slots__ = ('name', 'type', 'load', 'exec', 'size', 'crc', 'first', 'gen')

    def __init__(self, name, type_, load, exec_, size, crc, first, gen):
        self.name, self.type, self.load, self.exec = name, type_, load, exec_
        self.size, self.crc, self.first, self.gen = size, crc, first, gen


class _Hdr:
    __slots__ = ('kind', 'flags', 'seq', 'next', 'used', 'gen', 'ecount', 'dcrc')


def _hdr_parse(h):
    if _u32(h, 0) != HDR_MAGIC:
        return None
    t = bytearray(h[:28])
    t[H_FLAGS] = 0xFF
    if crc32(t) != _u32(h, H_HCRC):
        return None
    o = _Hdr()
    o.kind, o.flags = h[H_KIND], h[H_FLAGS]
    o.seq, o.next, o.used = _u16(h, H_SEQ), _u16(h, H_NEXT), _u16(h, H_USED)
    o.gen, o.ecount, o.dcrc = _u32(h, H_GEN), _u32(h, H_ECOUNT), _u32(h, H_DCRC)
    return o if o.kind in (K_FIRST, K_CONT) else None


def _hdr_build(sec, kind, seq, nxt, used, gen, ecount, dcrc):
    _put32(sec, 0, HDR_MAGIC)
    sec[H_KIND] = kind
    sec[H_FLAGS] = 0xFF
    _put16(sec, H_SEQ, seq)
    _put16(sec, H_NEXT, nxt)
    _put16(sec, H_USED, used)
    _put32(sec, H_GEN, gen)
    _put32(sec, H_ECOUNT, ecount)
    _put32(sec, H_DCRC, dcrc)
    _put32(sec, 24, 0xFFFFFFFF)
    _put32(sec, H_HCRC, crc32(sec[:28]))


def sectors_for(size):
    if size <= FS_FIRST_DATA:
        return 1
    return 1 + (size - FS_FIRST_DATA + FS_CONT_DATA - 1) // FS_CONT_DATA


# ---- names -----------------------------------------------------------------------------------------
_NAME_OK = set(b'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-$#@!%&\'()')


def parse_name(text, default_ext=None):
    """'game.bin' / b'GAME' -> b'GAME    BIN', or None for a bad name."""
    if isinstance(text, str):
        text = text.encode('latin-1', 'replace')
    out = bytearray(b' ' * FS_NAME_LEN)
    i, n, ln = 0, 0, len(text)
    while i < ln and text[i] != 0x2E:
        c = text[i]
        if 0x61 <= c <= 0x7A:
            c -= 32
        if c not in _NAME_OK or n >= 8:
            return None
        out[n] = c
        n += 1
        i += 1
    if n == 0:
        return None
    if i < ln:
        i += 1
        e = 0
        while i < ln:
            c = text[i]
            i += 1
            if 0x61 <= c <= 0x7A:
                c -= 32
            if c not in _NAME_OK or e >= 3:
                return None
            out[8 + e] = c
            e += 1
    elif default_ext:
        ext = default_ext.encode() if isinstance(default_ext, str) else default_ext
        for k in range(min(3, len(ext))):
            out[8 + k] = ext[k]
    return bytes(out)


def format_name(name11):
    """b'GAME    BIN' -> 'GAME.BIN'"""
    name11 = bytes(name11)
    i = 0
    while i < 8 and name11[i:i + 1] != b' ':
        i += 1
    base = name11[:i]
    j = 8
    while j < 11 and name11[j:j + 1] != b' ':
        j += 1
    return (base + b'.' + name11[8:j]).decode('latin-1')


# ---- the file system -------------------------------------------------------------------------------
class FileSystem:
    def __init__(self, flash):
        if flash.size < 2 * FS_SECTOR_SIZE or flash.size % FS_SECTOR_SIZE:
            raise ValueError('bad flash size')
        self.dev = flash
        self.nsec = min(flash.size // FS_SECTOR_SIZE, FS_MAX_SECTORS)
        self.used = [0] * FS_MAX_SECTORS
        self.ecount = [0] * FS_MAX_SECTORS
        self.files = []                      # sorted by name
        self.next_gen = 1
        self.mount()

    # -- helpers ----------------------------------------------------------------------------------
    def _rd(self, off, n):
        return self.dev.read(off, n)

    def _sector_blank(self, s):
        base = s * FS_SECTOR_SIZE
        return self.dev.img[base:base + FS_SECTOR_SIZE].count(0xFF) == FS_SECTOR_SIZE

    def _flush_sector(self, s, img, need_erase):
        d = self.dev
        base = s * FS_SECTOR_SIZE
        if need_erase and d.erase(base) != 0:
            return FS_ERR_IO
        for pg in range(1, FS_SECTOR_SIZE // FS_PAGE_SIZE):
            p = bytes(img[pg * FS_PAGE_SIZE:(pg + 1) * FS_PAGE_SIZE])
            if p != BLANK_PAGE and d.program(base + pg * FS_PAGE_SIZE, p) != 0:
                return FS_ERR_IO
        pg0 = bytearray(img[:FS_PAGE_SIZE])
        pg0[:32] = b'\xff' * 32                                   # no header yet
        if bytes(pg0) != BLANK_PAGE and d.program(base, bytes(pg0)) != 0:
            return FS_ERR_IO
        if d.program(base, bytes(img[:FS_PAGE_SIZE])) != 0:
            return FS_ERR_IO
        return FS_OK

    def _mark_deleted(self, first):
        base = first * FS_SECTOR_SIZE
        pg = bytearray(self._rd(base, FS_PAGE_SIZE))
        pg[H_FLAGS] = 0x00
        return FS_OK if self.dev.program(base, bytes(pg)) == 0 else FS_ERR_IO

    def _chain_walk(self, e, want_list=False):
        """(ok, list_of_sectors)"""
        need = sectors_for(e.size)
        cur = e.first
        lst = []
        for i in range(need):
            lst.append(cur)
            if i == need - 1:
                break
            hd = _hdr_parse(self._rd(cur * FS_SECTOR_SIZE, 32))
            if hd is None:
                return False, lst
            nxt = hd.next
            if nxt >= self.nsec or nxt == cur:
                return False, lst
            hd = _hdr_parse(self._rd(nxt * FS_SECTOR_SIZE, 32))
            if hd is None or hd.kind != K_CONT or hd.seq != i + 1 or hd.gen != e.gen:
                return False, lst
            cur = nxt
        return True, lst

    def _find_index(self, name):
        for i, f in enumerate(self.files):
            if f.name == name:
                return i
        return -1

    def _insert_sorted(self, e):
        pos = len(self.files)
        while pos > 0 and self.files[pos - 1].name > e.name:
            pos -= 1
        self.files.insert(pos, e)

    # -- mount ----------------------------------------------------------------------------------------
    def mount(self):
        self.used = [0] * FS_MAX_SECTORS
        self.ecount = [0] * FS_MAX_SECTORS
        self.files = []
        for s in range(self.nsec):
            hd = _hdr_parse(self._rd(s * FS_SECTOR_SIZE, 32))
            if hd:
                self.ecount[s] = hd.ecount
        max_gen = 0
        for s in range(self.nsec):
            h = self._rd(s * FS_SECTOR_SIZE, 64)
            hd = _hdr_parse(h)
            if hd is None or hd.kind != K_FIRST:
                continue
            if hd.gen > max_gen:
                max_gen = hd.gen
            if hd.flags != 0xFF:
                continue                                          # deleted
            e = Entry(h[M_NAME:M_NAME + FS_NAME_LEN], h[M_TYPE], _u16(h, M_LOAD), _u16(h, M_EXEC),
                      _u32(h, M_SIZE), _u32(h, M_CRC), s, hd.gen)
            if e.size > 0x00FFFFFF or not self._chain_walk(e)[0]:
                continue                                          # torn / orphaned
            dup = self._find_index(e.name)
            if dup >= 0:                                          # crash while overwriting: newest wins
                if self.files[dup].gen >= e.gen:
                    self._mark_deleted(s)
                    continue
                self._mark_deleted(self.files[dup].first)
                del self.files[dup]
            self._insert_sorted(e)
        self.next_gen = max_gen + 1
        for e in self.files:
            ok, lst = self._chain_walk(e)
            if ok:
                for s in lst[:sectors_for(e.size)]:
                    self.used[s] = 1
        return len(self.files)

    # -- directory --------------------------------------------------------------------------------------
    def count(self):
        return len(self.files)

    def entry(self, index):
        return self.files[index] if 0 <= index < len(self.files) else None

    def find(self, name):
        i = self._find_index(bytes(name))
        return self.files[i] if i >= 0 else None

    def _free_sectors(self):
        return sum(1 for s in range(self.nsec) if not self.used[s])

    def free_bytes(self):
        return self._free_sectors() * FS_CONT_DATA

    def total_bytes(self):
        return self.nsec * FS_CONT_DATA

    # -- reading ------------------------------------------------------------------------------------------
    def read(self, e, offset, length):
        """(bytes_read or negative error, data)"""
        if offset > e.size:
            return FS_ERR_ARG, b''
        length = min(length, e.size - offset)
        out = bytearray()
        cur, pos, idx = e.first, 0, 0
        while len(out) < length:
            cap = FS_FIRST_DATA if idx == 0 else FS_CONT_DATA
            here = min(cap, e.size - pos)
            start = offset + len(out)
            if start < pos + here:
                inner = start - pos
                n = min(here - inner, length - len(out))
                sec_off = (64 if idx == 0 else 32) + inner
                out += self._rd(cur * FS_SECTOR_SIZE + sec_off, n)
            if len(out) >= length:
                break
            hd = _hdr_parse(self._rd(cur * FS_SECTOR_SIZE, 32))
            if hd is None:
                return FS_ERR_CORRUPT, bytes(out)
            if hd.next >= self.nsec:
                return FS_ERR_CORRUPT, bytes(out)
            pos += here
            cur = hd.next
            idx += 1
        return len(out), bytes(out)

    # -- writing -------------------------------------------------------------------------------------------
    def writer_begin(self, name, type_, load, exec_, total):
        w = Writer(self)
        return self.begin(w, name, type_, load, exec_, total), w

    def begin(self, w, name, type_, load, exec_, total):
        w.active = False
        if total > 0x00FFFFFF:
            return FS_ERR_ARG
        need = sectors_for(total)
        if need > FS_MAX_SECTORS:
            return FS_ERR_FULL
        freeb = self._free_sectors()
        old = self._find_index(bytes(name))
        old_n = sectors_for(self.files[old].size) if old >= 0 else 0
        if need > freeb:
            if old >= 0 and need <= freeb + old_n:                # not room for both: drop the old one first
                r = self.delete(name)
                if r != FS_OK:
                    return r
                old = -1
            else:
                return FS_ERR_FULL
        w.old_first = self.files[old].first if old >= 0 else -1
        lst = []
        for k in range(need):
            best = -1
            for s in range(self.nsec):
                if self.used[s]:
                    continue
                if best < 0 or self.ecount[s] < self.ecount[best]:
                    best = s
            if best < 0:
                for j in lst:
                    self.used[j] = 0
                return FS_ERR_FULL
            self.used[best] = 1
            lst.append(best)
        w.list = lst
        w.nsec = need
        w.name, w.type, w.load, w.exec = bytes(name), type_, load, exec_
        w.total, w.written, w.crc = total, 0, 0
        w.gen = self.next_gen
        self.next_gen += 1
        w.cur, w.fill = 1, 0
        w.first_buf = bytearray(b'\xff' * FS_SECTOR_SIZE)
        w.buf = bytearray(b'\xff' * FS_SECTOR_SIZE)
        w.active = True
        return FS_OK

    def _write_sector(self, s, img, kind, seq, nxt, used, gen, dcrc):
        need_erase = not self._sector_blank(s)
        ec = self.ecount[s] + (1 if need_erase else 0)
        _hdr_build(img, kind, seq, nxt, used, gen, ec, dcrc)
        r = self._flush_sector(s, img, need_erase)
        if r == FS_OK:
            self.ecount[s] = ec
        return r

    def _flush_cont(self, w):
        nxt = w.list[w.cur + 1] if w.cur + 1 < w.nsec else NONE16
        dcrc = crc32(w.buf[32:32 + w.fill])
        r = self._write_sector(w.list[w.cur], w.buf, K_CONT, w.cur, nxt, w.fill, w.gen, dcrc)
        w.buf = bytearray(b'\xff' * FS_SECTOR_SIZE)
        w.fill = 0
        w.cur += 1
        return r

    def writer_write(self, w, data):
        data = bytes(data)
        ln = len(data)
        if not w.active or w.written + ln > w.total:
            return FS_ERR_ARG
        w.crc = crc32(data, w.crc)
        p = 0
        while ln:
            if w.written < FS_FIRST_DATA:
                n = min(FS_FIRST_DATA - w.written, ln)
                w.first_buf[64 + w.written:64 + w.written + n] = data[p:p + n]
                w.written += n
                p += n
                ln -= n
            else:
                n = min(FS_CONT_DATA - w.fill, ln)
                w.buf[32 + w.fill:32 + w.fill + n] = data[p:p + n]
                w.fill += n
                w.written += n
                p += n
                ln -= n
                if w.fill == FS_CONT_DATA:
                    r = self._flush_cont(w)
                    if r != FS_OK:
                        self.writer_abort(w)
                        return r
        return FS_OK

    def writer_finish(self, w):
        if not w.active or w.written != w.total:
            self.writer_abort(w)
            return FS_ERR_ARG
        if w.fill > 0:
            r = self._flush_cont(w)
            if r != FS_OK:
                self.writer_abort(w)
                return r
        f = w.first_buf
        f[M_NAME:M_NAME + FS_NAME_LEN] = w.name
        f[M_TYPE] = w.type
        _put16(f, M_LOAD, w.load)
        _put16(f, M_EXEC, w.exec)
        _put32(f, M_SIZE, w.total)
        _put32(f, M_CRC, w.crc)
        in_first = min(w.total, FS_FIRST_DATA)
        nxt = w.list[1] if w.nsec > 1 else NONE16
        r = self._write_sector(w.list[0], f, K_FIRST, 0, nxt, in_first, w.gen, crc32(f[64:64 + in_first]))
        if r != FS_OK:
            self.writer_abort(w)
            return r
        e = Entry(w.name, w.type, w.load, w.exec, w.total, w.crc, w.list[0], w.gen)
        if w.old_first >= 0:                                      # the new file is safe: retire the old one
            for i, old in enumerate(self.files):
                if old.first == w.old_first:
                    ok, lst = self._chain_walk(old)
                    n = sectors_for(old.size)
                    self._mark_deleted(old.first)
                    if ok:
                        for s in lst[:n]:
                            self.used[s] = 0
                    else:
                        self.used[old.first] = 0
                    del self.files[i]
                    break
        self._insert_sorted(e)
        w.active = False
        return FS_OK

    def writer_abort(self, w):
        if not w.active:
            return
        for s in w.list[:w.nsec]:
            self.used[s] = 0
        w.active = False

    def delete(self, name):
        idx = self._find_index(bytes(name))
        if idx < 0:
            return FS_ERR_NOTFOUND
        e = self.files[idx]
        ok, lst = self._chain_walk(e)
        r = self._mark_deleted(e.first)
        if r != FS_OK:
            return r
        if ok:
            for s in lst[:sectors_for(e.size)]:
                self.used[s] = 0
        else:
            self.used[e.first] = 0
        del self.files[idx]
        return FS_OK

    def format(self):
        while self.files:
            r = self.delete(self.files[-1].name)
            if r != FS_OK:
                return r
        return FS_OK

    def rename(self, old, new, w=None):
        w = w or Writer(self)
        si = self._find_index(bytes(old))
        if si < 0:
            return FS_ERR_NOTFOUND
        if self._find_index(bytes(new)) >= 0:
            return FS_ERR_EXISTS
        src = self.files[si]
        r = self.begin(w, new, src.type, src.load, src.exec, src.size)
        if r != FS_OK:
            return r
        off = 0
        while off < src.size:
            n = min(src.size - off, FS_PAGE_SIZE * 4)
            got, data = self.read(src, off, n)
            if got != n:
                self.writer_abort(w)
                return got if got < 0 else FS_ERR_IO
            r = self.writer_write(w, data)
            if r != FS_OK:
                return r
            off += n
        r = self.writer_finish(w)
        if r != FS_OK:
            return r
        return self.delete(old)

    def put_file(self, name, data, type_, load, exec_):
        """Whole-file write (what the PC side does)."""
        w = Writer(self)
        r = self.begin(w, name, type_, load, exec_, len(data))
        if r == FS_OK:
            r = self.writer_write(w, data)
        if r == FS_OK:
            r = self.writer_finish(w)
        else:
            self.writer_abort(w)
        return r


class Writer:
    def __init__(self, fs):
        self.fs = fs
        self.active = False
        self.written = self.total = self.crc = 0


# ---- the mailbox executor (src/storage_core.c) -----------------------------------------------------------
STO_CMD, STO_STATUS = 0x00, 0x01
STO_ADDR_L, STO_ADDR_H, STO_LEN_L, STO_LEN_H = 0x02, 0x03, 0x04, 0x05
STO_NAME_L, STO_NAME_H, STO_NAMELEN, STO_TYPE = 0x06, 0x07, 0x08, 0x09
STO_EXEC_L, STO_EXEC_H, STO_INDEX, STO_FLAGS, STO_ID, STO_VERSION = 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
STO_NAME2_L, STO_NAME2_H, STO_NAME2LEN = 0x10, 0x11, 0x12
STO_SIZE_0, STO_SIZE_1, STO_SIZE_2 = 0x13, 0x14, 0x15
STO_REG_COUNT = 0x20
STO_ID_VALUE, STO_VERSION_NO, STO_BUSY = 0x53, 1, 0x80
STO_OK, STO_E_NOTFOUND, STO_E_FULL, STO_E_BADNAME, STO_E_BADTYPE, STO_E_IO = 0, 1, 2, 3, 4, 5
STO_E_BADCMD, STO_E_END, STO_E_ADDRESS, STO_E_EXISTS, STO_E_TOOBIG = 6, 7, 8, 9, 10
STO_E_CORRUPT, STO_E_NOSTORAGE, STO_E_COUNT = 11, 12, 13
STO_C_NOP, STO_C_LOAD, STO_C_SAVE, STO_C_DELETE, STO_C_DIRENT = 0, 1, 2, 3, 4
STO_C_INFO, STO_C_FREE, STO_C_RENAME, STO_C_FORMAT = 5, 6, 7, 8
STO_FORMAT_MAGIC = 0xF0A5
STO_F_ADDR = 0x01
STO_TYPE_BIN, STO_TYPE_BAS, STO_TYPE_TXT, STO_TYPE_ANY = 0, 1, 2, 0xFF

_RESULT_TEXT = ("OK", "FILE NOT FOUND", "DISK FULL", "BAD FILE NAME", "FILE TYPE MISMATCH", "I/O ERROR",
                "BAD COMMAND", "END OF DIRECTORY", "ADDRESS RANGE", "FILE EXISTS", "FILE TOO BIG",
                "FILE CORRUPT", "NO STORAGE")


def result_text(r):
    return _RESULT_TEXT[r] if 0 <= r < STO_E_COUNT else 'ERROR'


def result_from_fs(r):
    return {FS_OK: STO_OK, FS_ERR_NOTFOUND: STO_E_NOTFOUND, FS_ERR_FULL: STO_E_FULL, FS_ERR_BADNAME: STO_E_BADNAME,
            FS_ERR_CORRUPT: STO_E_CORRUPT, FS_ERR_EXISTS: STO_E_EXISTS, FS_ERR_ARG: STO_E_BADCMD}.get(r, STO_E_IO)


def regs_init(mem, off):
    mem[off:off + STO_REG_COUNT] = bytes(STO_REG_COUNT)
    mem[off + STO_ID] = STO_ID_VALUE
    mem[off + STO_VERSION] = STO_VERSION_NO


def _default_ext(t):
    return {STO_TYPE_BAS: 'BAS', STO_TYPE_TXT: 'TXT'}.get(t, 'BIN')


def execute(fs, mem, regs_off, ram_end, ready=True):
    """Run the command in the mailbox at mem[regs_off:]; ends by writing STO_STATUS."""
    R = regs_off

    def reg16(lo):
        return mem[R + lo] | (mem[R + lo + 1] << 8)

    def set16(lo, v):
        mem[R + lo] = v & 0xFF
        mem[R + lo + 1] = (v >> 8) & 0xFF

    def get_name(lo, lenreg, type_):
        p = reg16(lo)
        n = mem[R + lenreg]
        if n == 0 or p + n > 0x10000 or n >= 40:
            return None, STO_E_BADNAME
        name = parse_name(bytes(mem[p:p + n]), _default_ext(type_))
        return (name, STO_OK) if name else (None, STO_E_BADNAME)

    def report(e, addr):
        set16(STO_ADDR_L, addr)
        set16(STO_LEN_L, min(e.size, 0xFFFF))
        mem[R + STO_SIZE_0] = e.size & 0xFF
        mem[R + STO_SIZE_1] = (e.size >> 8) & 0xFF
        mem[R + STO_SIZE_2] = (e.size >> 16) & 0xFF
        mem[R + STO_TYPE] = e.type
        set16(STO_EXEC_L, e.exec)

    cmd = mem[R + STO_CMD]
    if cmd == STO_C_NOP:
        mem[R + STO_STATUS] = STO_OK
        return
    if not ready or fs is None:
        mem[R + STO_STATUS] = STO_E_NOSTORAGE
        return
    typ = mem[R + STO_TYPE]
    res = STO_OK

    if cmd in (STO_C_LOAD, STO_C_INFO):
        n1, res = get_name(STO_NAME_L, STO_NAMELEN, typ)
        if res == STO_OK:
            e = fs.find(n1)
            if e is None:
                res = STO_E_NOTFOUND
            elif typ != STO_TYPE_ANY and typ != e.type:
                res = STO_E_BADTYPE
            else:
                dest = reg16(STO_ADDR_L) if mem[R + STO_FLAGS] & STO_F_ADDR else e.load
                if cmd == STO_C_LOAD:
                    if e.size > 0xFFFF:
                        res = STO_E_TOOBIG
                    elif dest + e.size > ram_end + 1:
                        res = STO_E_ADDRESS
                    else:
                        got, data = fs.read(e, 0, e.size)
                        mem[dest:dest + len(data)] = data                 # the C code reads straight into memory
                        if got != e.size:
                            res = result_from_fs(got) if got < 0 else STO_E_IO
                        elif crc32(data) != e.crc:
                            res = STO_E_CORRUPT
                if res == STO_OK:
                    report(e, dest)

    elif cmd == STO_C_SAVE:
        n1, res = get_name(STO_NAME_L, STO_NAMELEN, typ)
        if res == STO_OK:
            addr, ln = reg16(STO_ADDR_L), reg16(STO_LEN_L)
            t = STO_TYPE_BIN if typ == STO_TYPE_ANY else typ
            if addr + ln > 0x10000:
                res = STO_E_ADDRESS
            else:
                w = Writer(fs)
                f = fs.begin(w, n1, t, addr, reg16(STO_EXEC_L), ln)
                if f == FS_OK:
                    f = fs.writer_write(w, mem[addr:addr + ln])
                if f == FS_OK:
                    f = fs.writer_finish(w)
                else:
                    fs.writer_abort(w)
                res = result_from_fs(f)

    elif cmd == STO_C_DELETE:
        n1, res = get_name(STO_NAME_L, STO_NAMELEN, typ)
        if res == STO_OK:
            res = result_from_fs(fs.delete(n1))

    elif cmd == STO_C_RENAME:
        n1, res = get_name(STO_NAME_L, STO_NAMELEN, typ)
        if res == STO_OK:
            n2, res = get_name(STO_NAME2_L, STO_NAME2LEN, typ)
        if res == STO_OK:
            res = result_from_fs(fs.rename(n1, n2))

    elif cmd == STO_C_DIRENT:
        e = fs.entry(mem[R + STO_INDEX])
        if e is None:
            res = STO_E_END
        else:
            p = reg16(STO_NAME_L)
            if p + 13 > 0x10000:
                res = STO_E_ADDRESS
            else:
                txt = format_name(e.name).encode('latin-1') + b'\0'
                mem[p:p + len(txt)] = txt
                report(e, e.load)

    elif cmd == STO_C_FREE:
        fb = fs.free_bytes()
        mem[R + STO_SIZE_0], mem[R + STO_SIZE_1], mem[R + STO_SIZE_2] = fb & 0xFF, (fb >> 8) & 0xFF, (fb >> 16) & 0xFF
        set16(STO_LEN_L, fs.count() & 0xFFFF)

    elif cmd == STO_C_FORMAT:
        if reg16(STO_ADDR_L) != STO_FORMAT_MAGIC:
            res = STO_E_BADCMD
        else:
            res = result_from_fs(fs.format())

    else:
        res = STO_E_BADCMD
    mem[R + STO_STATUS] = res & 0xFF


# ---- the USB protocol, device side (src/usb_proto.c) --------------------------------------------------------
PROTO_VERSION = 1
PROTO_MAX_CHUNK = 1024
PROTO_MAX_PAYLOAD = PROTO_MAX_CHUNK + 16
PROTO_LIST_PER_FRAME = 8
PROTO_ENTRY_SIZE = 24
PROTO_PUT_TIMEOUT_MS = 10000
PROTO_C_HELLO, PROTO_C_LIST, PROTO_C_READ, PROTO_C_PUT_BEGIN, PROTO_C_PUT_DATA = 1, 2, 3, 4, 5
PROTO_C_PUT_END, PROTO_C_DELETE, PROTO_C_RENAME, PROTO_C_FORMAT = 6, 7, 8, 9
PROTO_FORMAT_MAGIC = 0x464F524D
PROTO_E_SEQ, PROTO_E_CRC, PROTO_E_NOSESSION, PROTO_E_BUSY = 20, 21, 22, 23


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class ProtoServer:
    """Feed received bytes to rx(); the reply bytes are returned."""

    def __init__(self, fs, ready=True):
        self.fs = fs
        self.ready = ready
        self.put = Writer(fs)
        self.putting = False
        self.put_next = 0
        self.last_ms = 0
        self.seen_ms = 0
        self.state = 0
        self.buf = bytearray()
        self.len = 0
        self.crc = 0
        self.rxcrc = bytearray(2)
        self.out = bytearray()

    def _reply(self, cmd, status, data=b''):
        head = bytearray([0xA5, 0x5A, 0, 0, cmd | 0x80, status])
        _put16(head, 2, 2 + len(data))
        crc = crc16(bytes(head[2:6]))
        crc = crc16(data, crc)
        self.out += head + bytes(data) + bytes([crc & 0xFF, crc >> 8])

    def _abort_put(self):
        if self.putting:
            self.fs.writer_abort(self.put)
        self.putting = False

    def _handle(self, pl, now):
        fs = self.fs
        cmd, d = pl[0], bytes(pl[1:])
        dn = len(d)
        self.seen_ms = now
        if not self.ready and cmd != PROTO_C_HELLO:
            return self._reply(cmd, STO_E_NOSTORAGE)
        if cmd == PROTO_C_HELLO:
            out = bytearray(13)
            out[0] = PROTO_VERSION
            _put16(out, 1, PROTO_MAX_CHUNK)
            _put32(out, 3, fs.total_bytes() if self.ready else 0)
            _put32(out, 7, fs.free_bytes() if self.ready else 0)
            _put16(out, 11, fs.count() if self.ready else 0)
            self._reply(cmd, STO_OK if self.ready else STO_E_NOSTORAGE, bytes(out) + b'P65 storage\0')
        elif cmd == PROTO_C_LIST:
            if dn < 2:
                return self._reply(cmd, STO_E_BADCMD)
            start = _u16(d, 0)
            total = fs.count()
            body = bytearray()
            cnt = 0
            i = start
            while i < total and cnt < PROTO_LIST_PER_FRAME:
                e = fs.entry(i)
                o = bytearray(PROTO_ENTRY_SIZE)
                o[:11] = e.name
                o[11] = e.type
                _put16(o, 12, e.load)
                _put16(o, 14, e.exec)
                _put32(o, 16, e.size)
                _put32(o, 20, e.crc)
                body += o
                i += 1
                cnt += 1
            head = bytearray(3)
            head[0] = cnt
            _put16(head, 1, total)
            self._reply(cmd, STO_OK, bytes(head) + bytes(body))
        elif cmd == PROTO_C_READ:
            if dn < 17:
                return self._reply(cmd, STO_E_BADCMD)
            e = fs.find(d[:11])
            if e is None:
                return self._reply(cmd, STO_E_NOTFOUND)
            off, ln = _u32(d, 11), min(_u16(d, 15), PROTO_MAX_CHUNK)
            if off > e.size:
                return self._reply(cmd, STO_E_ADDRESS)
            got, data = fs.read(e, off, ln)
            if got < 0:
                return self._reply(cmd, result_from_fs(got))
            self._reply(cmd, STO_OK, data)
        elif cmd == PROTO_C_PUT_BEGIN:
            if dn < 20:
                return self._reply(cmd, STO_E_BADCMD)
            self._abort_put()
            r = fs.begin(self.put, d[:11], d[11], _u16(d, 12), _u16(d, 14), _u32(d, 16))
            if r == FS_OK:
                self.putting, self.put_next, self.last_ms = True, 0, now
            self._reply(cmd, result_from_fs(r))
        elif cmd == PROTO_C_PUT_DATA:
            if not self.putting:
                return self._reply(cmd, PROTO_E_NOSESSION)
            if dn < 4:
                return self._reply(cmd, STO_E_BADCMD)
            if _u32(d, 0) != self.put_next:
                return self._reply(cmd, PROTO_E_SEQ)
            r = fs.writer_write(self.put, d[4:])
            if r != FS_OK:
                self.putting = False
                return self._reply(cmd, result_from_fs(r))
            self.put_next += dn - 4
            self.last_ms = now
            self._reply(cmd, STO_OK)
        elif cmd == PROTO_C_PUT_END:
            if not self.putting:
                return self._reply(cmd, PROTO_E_NOSESSION)
            if dn < 4:
                return self._reply(cmd, STO_E_BADCMD)
            if self.put.written != self.put.total or _u32(d, 0) != self.put.crc:
                self._abort_put()
                return self._reply(cmd, PROTO_E_CRC)
            r = fs.writer_finish(self.put)
            self.putting = False
            self._reply(cmd, result_from_fs(r))
        elif cmd == PROTO_C_DELETE:
            if dn < FS_NAME_LEN:
                return self._reply(cmd, STO_E_BADCMD)
            self._reply(cmd, result_from_fs(fs.delete(d[:11])))
        elif cmd == PROTO_C_RENAME:
            if dn < 2 * FS_NAME_LEN:
                return self._reply(cmd, STO_E_BADCMD)
            self._reply(cmd, result_from_fs(fs.rename(d[:11], d[11:22])))
        elif cmd == PROTO_C_FORMAT:
            if dn < 4 or _u32(d, 0) != PROTO_FORMAT_MAGIC:
                return self._reply(cmd, STO_E_BADCMD)
            self._abort_put()
            self._reply(cmd, result_from_fs(fs.format()))
        else:
            self._reply(cmd, STO_E_BADCMD)

    def rx(self, data, now_ms=0):
        self.out = bytearray()
        for b in data:
            st = self.state
            if st == 0:
                if b == 0xA5:
                    self.state = 1
            elif st == 1:
                self.state = 2 if b == 0x5A else (1 if b == 0xA5 else 0)
            elif st == 2:
                self.len = b
                self.state = 3
            elif st == 3:
                self.len |= b << 8
                if self.len < 1 or self.len > PROTO_MAX_PAYLOAD:
                    self.state = 0
                else:
                    self.crc = crc16(bytes([self.len & 0xFF, self.len >> 8]))
                    self.buf = bytearray()
                    self.state = 4
            elif st == 4:
                self.buf.append(b)
                if len(self.buf) == self.len:
                    self.crc = crc16(bytes(self.buf), self.crc)
                    self.state = 5
            elif st == 5:
                self.rxcrc[0] = b
                self.state = 6
            elif st == 6:
                self.rxcrc[1] = b
                self.state = 0
                if _u16(self.rxcrc, 0) == self.crc:
                    self._handle(bytes(self.buf), now_ms)
        self.tick(now_ms)
        return bytes(self.out)

    def tick(self, now_ms):
        if self.putting and now_ms - self.last_ms > PROTO_PUT_TIMEOUT_MS:
            self._abort_put()
