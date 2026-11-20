"""Storage for the emulator: the firmware's flash file system, mailbox executor
and file format, loaded from p65store.dll (built from the very same C sources).

The "flash" is an image file (default emulator/storage.img, 1 MB like the
device). The CPU reaches it through the same mailbox registers as on the
device (see include/storage.h); the PC side - and the USB manager - use the
file level API of Store.
"""

import ctypes
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = '.dll' if sys.platform == 'win32' else ('.dylib' if sys.platform == 'darwin' else '.so')
DLL_PATH = os.path.join(HERE, 'p65store' + EXT)
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
    """A flash image mounted through the firmware's own file system code."""

    def __init__(self, path=DEFAULT_IMAGE, size=DEFAULT_SIZE):
        if not os.path.exists(DLL_PATH):
            raise StorageError(-1, 'p65store%s not found - run: python build_storelib.py' % EXT)
        self._lib = lib = ctypes.CDLL(DLL_PATH)
        P, U8P = ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8)
        lib.p65s_open.restype = P
        lib.p65s_open.argtypes = [ctypes.c_char_p, ctypes.c_uint32]
        lib.p65s_close.argtypes = [P]
        lib.p65s_flush.argtypes = [P]
        lib.p65s_exec.argtypes = [P, U8P, ctypes.c_uint32, ctypes.c_uint32]
        lib.p65s_ops.argtypes = [P, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        lib.p65s_init_regs.argtypes = [U8P]
        lib.p65s_count.argtypes = [P]
        lib.p65s_free.restype = ctypes.c_uint32
        lib.p65s_free.argtypes = [P]
        lib.p65s_total.restype = ctypes.c_uint32
        lib.p65s_total.argtypes = [P]
        lib.p65s_entry.argtypes = [P, ctypes.c_int, ctypes.c_char_p]
        lib.p65s_read.argtypes = [P, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
        lib.p65s_write.argtypes = [P, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        lib.p65s_delete.argtypes = [P, ctypes.c_char_p]
        lib.p65s_rename.argtypes = [P, ctypes.c_char_p, ctypes.c_char_p]
        lib.p65s_format.argtypes = [P]
        lib.p65s_proto.restype = ctypes.c_size_t
        lib.p65s_proto.argtypes = [P, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
        lib.p65s_parse_name.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
        lib.p65s_format_name.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.p65s_crc32.restype = ctypes.c_uint32
        lib.p65s_crc32.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
        lib.p65s_result_text.restype = ctypes.c_char_p
        lib.p65s_result_text.argtypes = [ctypes.c_int]
        self.path = path
        self.size = size
        self._h = lib.p65s_open((path or '').encode(), size)
        if not self._h:
            raise StorageError(-1, 'cannot open storage image %s' % path)

    # -- lifetime -------------------------------------------------------------
    def close(self):
        if self._h:
            self._lib.p65s_close(self._h)
            self._h = None

    def flush(self):
        if self._h:
            self._lib.p65s_flush(self._h)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # -- names ------------------------------------------------------------------
    def parse_name(self, text, default_ext=None):
        """'game.bin' -> b'GAME    BIN' (raises StorageError(3) for a bad name)."""
        raw = text.encode('ascii', 'replace')
        out = ctypes.create_string_buffer(11)
        if self._lib.p65s_parse_name(raw, len(raw), default_ext.encode() if default_ext else None, out) != 0:
            raise StorageError(3, 'bad file name: %r' % text)
        return out.raw[:11]

    def format_name(self, name11):
        out = ctypes.create_string_buffer(13)
        self._lib.p65s_format_name(bytes(name11), out)
        return out.value.decode('ascii')

    def result_text(self, code):
        return self._lib.p65s_result_text(code).decode()

    @staticmethod
    def crc32(data, crc=0):
        return Store._crc(data, crc)

    # -- file level API -----------------------------------------------------------
    def entries(self):
        """List of dicts: name ('GAME.BIN'), name11, type, load, exec, size, crc."""
        res = []
        buf = ctypes.create_string_buffer(24)
        for i in range(self._lib.p65s_count(self._h)):
            if self._lib.p65s_entry(self._h, i, buf) != 0:
                break
            name11, typ, load, exe, size, crc = struct.unpack('<11sBHHII', buf.raw)
            res.append(dict(name=self.format_name(name11), name11=name11, type=typ, load=load, exec=exe, size=size, crc=crc))
        return res

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
        buf = ctypes.create_string_buffer(max(e['size'], 1))
        got = self._lib.p65s_read(self._h, e['name11'], 0, buf, e['size'])
        if got != e['size']:
            raise StorageError(5, 'read failed')
        return buf.raw[:e['size']]

    def write(self, name, data, ftype=TYPE_BIN, load=0, exec_addr=0):
        n11 = name if isinstance(name, bytes) else self.parse_name(name, TYPE_NAMES.get(ftype))
        data = bytes(data)
        r = self._lib.p65s_write(self._h, n11, ftype, load, exec_addr, data, len(data))
        self.flush()
        if r != 0:
            raise StorageError({-3: 2, -4: 3}.get(r, 5), 'write failed (%d)' % r)

    def delete(self, name):
        n11 = name if isinstance(name, bytes) else self.parse_name(name)
        r = self._lib.p65s_delete(self._h, n11)
        self.flush()
        if r != 0:
            raise StorageError(1 if r == -2 else 5, 'delete failed')

    def rename(self, old, new):
        a = old if isinstance(old, bytes) else self.parse_name(old)
        b = new if isinstance(new, bytes) else self.parse_name(new)
        r = self._lib.p65s_rename(self._h, a, b)
        self.flush()
        if r != 0:
            raise StorageError({-2: 1, -7: 9, -3: 2}.get(r, 5), 'rename failed (%d)' % r)

    def format(self):
        self._lib.p65s_format(self._h)
        self.flush()

    def free_bytes(self):
        return self._lib.p65s_free(self._h)

    def total_bytes(self):
        return self._lib.p65s_total(self._h)

    # -- the CPU's mailbox ------------------------------------------------------------
    def execute(self, mem, ram_end, regs_off):
        """Run the command in the mailbox. mem: the CPU's bytearray (>= 64 KB).
        Returns (erases, programs) done, to time the command."""
        arr = (ctypes.c_uint8 * len(mem)).from_buffer(mem)
        self._lib.p65s_exec(self._h, arr, ram_end, regs_off)
        er, pr = ctypes.c_uint32(), ctypes.c_uint32()
        self._lib.p65s_ops(self._h, ctypes.byref(er), ctypes.byref(pr))
        self.flush()
        return er.value, pr.value

    def init_regs(self, mem, regs_off):
        arr = (ctypes.c_uint8 * len(mem)).from_buffer(mem)
        self._lib.p65s_init_regs(ctypes.cast(ctypes.addressof(arr) + regs_off, ctypes.POINTER(ctypes.c_uint8)))

    # -- the USB protocol, device side, as a pure function (used by the tests) ---------
    def protocol(self, data, now_ms=0):
        out = ctypes.create_string_buffer(8192)
        n = self._lib.p65s_proto(self._h, bytes(data), len(data), out, len(out), int(now_ms))
        return out.raw[:n]


def _crc32_fallback(data, crc=0):
    import zlib
    return zlib.crc32(data, crc) & 0xFFFFFFFF


Store._crc = staticmethod(_crc32_fallback)      # same polynomial as the firmware (checked by the tests)


def op_time_ms(erases, programs, nbytes=0):
    """How long the device needs for a command that erased/programmed this much."""
    return erases * ERASE_MS + programs * PROGRAM_MS + nbytes / READ_BYTES_PER_MS
