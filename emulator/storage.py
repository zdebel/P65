"""Storage for the emulator: the firmware's flash file system, mailbox executor and file
format, ported to pure Python (p65fs.py; tests/test_py_vs_c.py compares it with the C code).

The "flash" is an image file (default emulator/storage.img, 1 MB like the
device). The CPU reaches it through the same mailbox registers as on the
device (see include/storage.h); the PC side - and the USB manager - use the
file level API of Store.
"""

import os

import p65fs as P

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IMAGE = os.path.join(HERE, 'storage.img')
DEFAULT_SIZE = 1024 * 1024

# --- mailbox: must match include/storage.h (tests/test_storage.py checks it) ------
STO_CMD, STO_STATUS = 0x00, 0x01
STO_ADDR_L, STO_ADDR_H, STO_LEN_L, STO_LEN_H = 0x02, 0x03, 0x04, 0x05
STO_NAME_L, STO_NAME_H, STO_NAMELEN, STO_TYPE = 0x06, 0x07, 0x08, 0x09
STO_EXEC_L, STO_EXEC_H, STO_INDEX, STO_FLAGS, STO_ID, STO_VERSION = 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
STO_NAME2_L, STO_NAME2_H, STO_NAME2LEN = 0x10, 0x11, 0x12
STO_SIZE_0, STO_SIZE_1, STO_SIZE_2 = 0x13, 0x14, 0x15
STO_REG_COUNT = 0x20
STO_ID_VALUE = 0x53
STO_VERSION_NO = 1
STO_BUSY = 0x80
STO_OK = 0
STO_E_NOTFOUND, STO_E_FULL, STO_E_BADNAME, STO_E_BADTYPE, STO_E_IO = 1, 2, 3, 4, 5
STO_E_BADCMD, STO_E_END, STO_E_ADDRESS, STO_E_EXISTS, STO_E_TOOBIG = 6, 7, 8, 9, 10
STO_E_CORRUPT, STO_E_NOSTORAGE, STO_E_COUNT = 11, 12, 13

STO_C_NOP, STO_C_LOAD, STO_C_SAVE, STO_C_DELETE, STO_C_DIRENT = 0, 1, 2, 3, 4
STO_C_INFO, STO_C_FREE, STO_C_RENAME, STO_C_FORMAT = 5, 6, 7, 8
STO_FORMAT_MAGIC = 0xF0A5
STO_F_ADDR = 0x01
TYPE_BIN, TYPE_BAS, TYPE_TXT, TYPE_ANY = 0, 1, 2, 0xFF
STO_TYPE_BIN, STO_TYPE_BAS, STO_TYPE_TXT, STO_TYPE_ANY = TYPE_BIN, TYPE_BAS, TYPE_TXT, TYPE_ANY
TYPE_NAMES = {TYPE_BIN: 'BIN', TYPE_BAS: 'BAS', TYPE_TXT: 'TXT'}

# flash timing of the device, used to make the emulated commands take realistic time
ERASE_MS = 45.0         # one 4 KB sector
PROGRAM_MS = 0.4        # one 256 byte page
READ_BYTES_PER_MS = 20000.0


class StorageError(Exception):
    def __init__(self, code, text=None):
        super().__init__(text or 'storage error %d' % code)
        self.code = code


class Store:
    """A flash image with the P65 file system (pure Python, see p65fs.py)."""

    def __init__(self, path=DEFAULT_IMAGE, size=DEFAULT_SIZE):
        if size < 2 * P.FS_SECTOR_SIZE or size % P.FS_SECTOR_SIZE:
            raise StorageError(-1, 'bad storage size %d' % size)
        image = None
        if path and os.path.exists(path):
            with open(path, 'rb') as f:
                image = f.read(size)
        self.path = path
        self.size = size
        self.flash = P.Flash(size, image)
        try:
            self.fs = P.FileSystem(self.flash)
        except (ValueError, IOError) as e:
            raise StorageError(-1, 'cannot open storage image %s: %s' % (path, e))
        self.proto = P.ProtoServer(self.fs)
        self._ops_reset()

    def _ops_reset(self):
        self.flash.erases = self.flash.programs = 0

    # -- lifetime -------------------------------------------------------------
    def close(self):
        self.flush()

    def flush(self):
        if self.path and self.flash.dirty:
            with open(self.path, 'wb') as f:
                f.write(self.flash.img)
            self.flash.dirty = False

    # -- names ------------------------------------------------------------------
    def parse_name(self, text, default_ext=None):
        """'game.bin' -> b'GAME    BIN' (raises StorageError(3) for a bad name)."""
        n = P.parse_name(text.encode('ascii', 'replace'), default_ext)
        if n is None:
            raise StorageError(3, 'bad file name: %r' % text)
        return n

    def format_name(self, name11):
        return P.format_name(name11)

    def result_text(self, code):
        return P.result_text(code)

    @staticmethod
    def crc32(data, crc=0):
        return P.crc32(data, crc)

    # -- file level API -----------------------------------------------------------
    def entries(self):
        """List of dicts: name ('GAME.BIN'), name11, type, load, exec, size, crc."""
        return [dict(name=P.format_name(e.name), name11=e.name, type=e.type, load=e.load, exec=e.exec,
                     size=e.size, crc=e.crc) for e in self.fs.files]

    def find(self, name):
        n11 = name if isinstance(name, bytes) else self.parse_name(name)
        for e in self.entries():
            if e['name11'] == n11:
                return e
        return None

    def read(self, name):
        e = self.find(name)
        if e is None:
            raise StorageError(1, 'file not found: %s' % (name,))
        got, data = self.fs.read(self.fs.find(e['name11']), 0, e['size'])
        if got != e['size']:
            raise StorageError(5, 'read failed')
        return data

    def write(self, name, data, ftype=TYPE_BIN, load=0, exec_addr=0):
        n11 = name if isinstance(name, bytes) else self.parse_name(name, TYPE_NAMES.get(ftype))
        r = self.fs.put_file(n11, bytes(data), ftype, load, exec_addr)
        self.flush()
        if r != 0:
            raise StorageError({-3: 2, -4: 3}.get(r, 5), 'write failed (%d)' % r)

    def delete(self, name):
        n11 = name if isinstance(name, bytes) else self.parse_name(name)
        r = self.fs.delete(n11)
        self.flush()
        if r != 0:
            raise StorageError(1 if r == -2 else 5, 'delete failed')

    def rename(self, old, new):
        a = old if isinstance(old, bytes) else self.parse_name(old)
        b = new if isinstance(new, bytes) else self.parse_name(new)
        r = self.fs.rename(a, b)
        self.flush()
        if r != 0:
            raise StorageError({-2: 1, -7: 9, -3: 2}.get(r, 5), 'rename failed (%d)' % r)

    def format(self):
        self.fs.format()
        self.flush()

    def free_bytes(self):
        return self.fs.free_bytes()

    def total_bytes(self):
        return self.fs.total_bytes()

    # -- the CPU's mailbox ------------------------------------------------------------
    def execute(self, mem, ram_end, regs_off):
        """Run the command in the mailbox. mem: the CPU's bytearray (>= 64 KB).
        Returns (erases, programs) done, to time the command."""
        self._ops_reset()
        P.execute(self.fs, mem, regs_off, ram_end)
        er, pr = self.flash.erases, self.flash.programs
        self.flush()
        return er, pr

    def init_regs(self, mem, regs_off):
        P.regs_init(mem, regs_off)

    # -- the USB protocol, device side, as a pure function (used by the tests) ---------
    def protocol(self, data, now_ms=0):
        return self.proto.rx(bytes(data), int(now_ms))


def _crc32_fallback(data, crc=0):
    import zlib
    return zlib.crc32(data, crc) & 0xFFFFFFFF


Store._crc = staticmethod(_crc32_fallback)      # same polynomial as the firmware (checked by the tests)


def op_time_ms(erases, programs, nbytes=0):
    """How long the device needs for a command that erased/programmed this much."""
    return erases * ERASE_MS + programs * PROGRAM_MS + nbytes / READ_BYTES_PER_MS
