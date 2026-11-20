"""Client for the P65 USB storage protocol (see src/usb_proto.h).

Talks to the device through any object with write(bytes) and read(n) -> bytes
(returning b'' on timeout): a pyserial port, or LoopbackTransport(store) which
runs the firmware's own protocol code from p65store.dll (used by the tests).
"""

import struct
import time

SYNC = b'\xA5\x5A'
C_HELLO, C_LIST, C_READ, C_PUT_BEGIN, C_PUT_DATA, C_PUT_END = 1, 2, 3, 4, 5, 6
C_DELETE, C_RENAME, C_FORMAT = 7, 8, 9
FORMAT_MAGIC = 0x464F524D

TYPE_BIN, TYPE_BAS, TYPE_TXT = 0, 1, 2
TYPE_NAMES = {TYPE_BIN: 'BIN', TYPE_BAS: 'BAS', TYPE_TXT: 'TXT'}

STATUS_TEXT = {
    0: 'OK', 1: 'file not found', 2: 'disk full', 3: 'bad file name', 4: 'file type mismatch', 5: 'I/O error',
    6: 'bad command', 7: 'end of directory', 8: 'address out of range', 9: 'file exists', 10: 'file too big',
    11: 'file corrupt', 12: 'no storage on the device', 20: 'data out of sequence', 21: 'CRC mismatch',
    22: 'no transfer in progress', 23: 'device busy',
}


class DeviceError(Exception):
    def __init__(self, status, what=''):
        self.status = status
        super().__init__('%s%s' % (what + ': ' if what else '', STATUS_TEXT.get(status, 'error %d' % status)))


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc32(data, crc=0):
    import zlib
    return zlib.crc32(data, crc) & 0xFFFFFFFF


_NAME_OK = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-$#@!%&\'()')


def parse_name(text, default_ext=None):
    """'game.bin' -> b'GAME    BIN'. Same rules as fs_parse_name() in the firmware."""
    text = text.upper()
    base, dot, ext = text.partition('.')
    if not base or len(base) > 8 or len(ext) > 3 or any(c not in _NAME_OK for c in base + ext) or '.' in ext:
        raise ValueError('bad file name: %r' % text)
    if not dot and default_ext:
        ext = default_ext[:3].upper()
    return base.ljust(8).encode('ascii') + ext.ljust(3).encode('ascii')


def format_name(name11):
    s = bytes(name11).decode('ascii', 'replace')
    return s[:8].rstrip() + '.' + s[8:11].rstrip()


class LoopbackTransport:
    """In-process 'serial port' connected to a Store's protocol handler."""

    def __init__(self, store):
        self.store = store
        self.rx = bytearray()
        self.now = 0

    def write(self, data):
        self.now += 1
        self.rx += self.store.protocol(data, self.now)

    def read(self, n):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def reset_input_buffer(self):
        self.rx.clear()


class P65Device:
    def __init__(self, transport, timeout=3.0):
        self.t = transport
        self.timeout = timeout
        self.max_chunk = 1024

    # -- framing ----------------------------------------------------------------
    @staticmethod
    def frame(payload):
        head = struct.pack('<H', len(payload))
        return SYNC + head + payload + struct.pack('<H', crc16(head + payload))

    def _read_exact(self, n, deadline):
        buf = b''
        while len(buf) < n:
            chunk = self.t.read(n - len(buf))
            if chunk:
                buf += chunk
            elif time.monotonic() > deadline:
                raise TimeoutError('no answer from the device')
            else:
                time.sleep(0.001)
        return buf

    def _read_frame(self):
        deadline = time.monotonic() + self.timeout
        state = 0
        while True:                          # hunt for the sync bytes, skipping any text
            b = self._read_exact(1, deadline)
            if state == 0:
                state = 1 if b == b'\xA5' else 0
            elif b == b'\x5A':
                break
            else:
                state = 1 if b == b'\xA5' else 0
        head = self._read_exact(2, deadline)
        (n,) = struct.unpack('<H', head)
        body = self._read_exact(n + 2, deadline)
        payload, (crc,) = body[:n], struct.unpack('<H', body[n:])
        if crc != crc16(head + payload):
            raise DeviceError(21, 'corrupt reply')
        return payload

    def request(self, cmd, data=b'', check=True):
        self.t.write(self.frame(bytes([cmd]) + data))
        payload = self._read_frame()
        if len(payload) < 2 or payload[0] != (cmd | 0x80):
            raise DeviceError(6, 'unexpected reply')
        status, body = payload[1], payload[2:]
        if check and status != 0:
            raise DeviceError(status)
        return status, body

    # -- commands ------------------------------------------------------------------
    def hello(self):
        if hasattr(self.t, 'reset_input_buffer'):
            self.t.reset_input_buffer()
        _, d = self.request(C_HELLO)
        ver, chunk, total, free, files = struct.unpack('<BHIIH', d[:13])
        self.max_chunk = max(64, min(chunk, 1024))
        return dict(version=ver, max_chunk=chunk, total=total, free=free, files=files,
                    text=d[13:].split(b'\0')[0].decode('ascii', 'replace'))

    def list(self):
        entries, start = [], 0
        while True:
            _, d = self.request(C_LIST, struct.pack('<H', start))
            n, total = d[0], struct.unpack('<H', d[1:3])[0]
            for i in range(n):
                name11, typ, load, exe, size, crc = struct.unpack('<11sBHHII', d[3 + i * 24:3 + (i + 1) * 24])
                entries.append(dict(name=format_name(name11), name11=name11, type=typ, load=load, exec=exe, size=size, crc=crc))
            start += n
            if n == 0 or start >= total:
                return entries

    def read_file(self, name11, size=None, progress=None):
        if size is None:
            size = next(e['size'] for e in self.list() if e['name11'] == name11)
        out = bytearray()
        while len(out) < size:
            _, d = self.request(C_READ, name11 + struct.pack('<IH', len(out), min(self.max_chunk, size - len(out))))
            if not d:
                raise DeviceError(5, 'short read')
            out += d
            if progress:
                progress(len(out), size)
        return bytes(out)

    def put_file(self, name11, data, ftype=TYPE_BIN, load=0, exec_addr=0, progress=None):
        data = bytes(data)
        self.request(C_PUT_BEGIN, name11 + struct.pack('<BHHI', ftype, load, exec_addr, len(data)))
        off = 0
        while off < len(data):
            chunk = data[off:off + self.max_chunk]
            self.request(C_PUT_DATA, struct.pack('<I', off) + chunk)
            off += len(chunk)
            if progress:
                progress(off, len(data))
        self.request(C_PUT_END, struct.pack('<I', crc32(data)))

    def delete(self, name11):
        self.request(C_DELETE, name11)

    def rename(self, old11, new11):
        self.request(C_RENAME, old11 + new11)

    def format(self):
        self.request(C_FORMAT, struct.pack('<I', FORMAT_MAGIC))


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, p.description) for p in list_ports.comports()]


def open_serial(port, baud=115200):
    import serial
    return serial.Serial(port, baud, timeout=0.05, write_timeout=2)
