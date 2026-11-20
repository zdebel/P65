"""The logic behind the storage manager (no Qt): two interchangeable backends -
the emulator's own storage image and the device over USB - plus import /
export / viewing of files. Tested without a GUI (tests/test_storageio.py)."""

import os

import basictok
import p65dev
import storage as S

TYPE_LABEL = {S.TYPE_BIN: 'BIN', S.TYPE_BAS: 'BAS', S.TYPE_TXT: 'TXT'}


class LocalBackend:
    """The emulator's storage image (the one the emulated CPU sees)."""

    title = 'Emulator storage'

    def __init__(self, store):
        self.store = store

    def list(self):
        return self.store.entries()

    def info(self):
        return dict(total=self.store.total_bytes(), free=self.store.free_bytes(), files=len(self.store.entries()))

    def read(self, entry, progress=None):
        data = self.store.read(entry['name11'])
        if progress:
            progress(len(data), len(data))
        return data

    def write(self, name11, data, ftype, load, exec_addr, progress=None):
        self.store.write(name11, data, ftype, load, exec_addr)
        if progress:
            progress(len(data), len(data))

    def delete(self, name11):
        self.store.delete(name11)

    def rename(self, old11, new11):
        self.store.rename(old11, new11)

    def format(self):
        self.store.format()


class DeviceBackend:
    """The P65 hardware over its USB serial port."""

    title = 'Device (USB)'

    def __init__(self, dev):
        self.dev = dev

    def list(self):
        return self.dev.list()

    def info(self):
        h = self.dev.hello()
        return dict(total=h['total'], free=h['free'], files=h['files'])

    def read(self, entry, progress=None):
        return self.dev.read_file(entry['name11'], entry['size'], progress)

    def write(self, name11, data, ftype, load, exec_addr, progress=None):
        self.dev.put_file(name11, data, ftype, load, exec_addr, progress)

    def delete(self, name11):
        self.dev.delete(name11)

    def rename(self, old11, new11):
        self.dev.rename(old11, new11)

    def format(self):
        self.dev.format()


def copy_file(src, dst, entry, progress=None):
    """Copy one file between backends, keeping its type and addresses."""
    data = src.read(entry, progress)
    dst.write(entry['name11'], data, entry['type'], entry['load'], entry['exec'], progress)
    return data


# ---------------------------------------------------------------- host files -> storage
def guess_name(path, ext):
    """A valid 8.3 name for a host file: 'My Game v2.bin' -> 'MYGAMEV2.BIN'."""
    base = os.path.splitext(os.path.basename(path))[0].upper()
    base = ''.join(c for c in base if c in p65dev._NAME_OK)[:8] or 'FILE'
    return p65dev.parse_name(base + '.' + ext)


def looks_tokenized(data):
    """True if the bytes are a valid tokenized program (links chain, ends with a zero link)."""
    pos = 0
    try:
        while True:
            if pos + 2 > len(data):
                return False
            link = data[pos] | data[pos + 1] << 8
            if link >> 8 == 0:                          # the end marker
                return True
            if pos + 4 > len(data):
                return False
            if link <= basictok.TXTTAB + pos or link - basictok.TXTTAB > len(data):
                return False
            end = data.index(0, pos + 4)
            if basictok.TXTTAB + end + 1 != link:
                return False
            pos = link - basictok.TXTTAB
    except ValueError:
        return False


def import_host_file(path, ftype=None, load=None, exec_addr=0):
    """Read a file from the PC. Returns dict(name11, data, type, load, exec).

    .BAS / .TXT that look like BASIC source (lines starting with a number) are
    tokenized into a program; an already tokenized .BAS is taken as is; .TXT
    stays text; everything else is a binary (load defaults to 0x0800)."""
    with open(path, 'rb') as f:
        raw = f.read()
    ext = os.path.splitext(path)[1].lstrip('.').upper()
    if ftype is None:
        if ext == 'BAS':
            ftype = S.TYPE_BAS
        elif ext == 'TXT':
            ftype = S.TYPE_TXT
        else:
            ftype = S.TYPE_BIN
    data = raw
    if ftype == S.TYPE_BAS and not looks_tokenized(raw):
        text = raw.decode('latin-1')
        data = basictok.tokenize_program(text)               # may raise ValueError (no line numbers)
        load = basictok.TXTTAB
    elif ftype == S.TYPE_BAS:
        load = basictok.TXTTAB
    if load is None:
        load = 0x0800
    if len(data) > 0xFFFFFF:
        raise ValueError('file too big')
    return dict(name11=guess_name(path, TYPE_LABEL.get(ftype, 'BIN')), data=data, type=ftype, load=load, exec=exec_addr)


def export_file(entry, data, as_text=True):
    """(suggested file name, bytes) for saving a storage file on the PC."""
    name = p65dev.format_name(entry['name11']).lower()
    if entry['type'] == S.TYPE_BAS and as_text:
        return name, basictok.detokenize_program(data).encode('latin-1', 'replace')
    return name, data


def hexdump(data, limit=4096):
    rows = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        rows.append('%04X  %-47s  %s' % (off, ' '.join('%02X' % b for b in chunk),
                                        ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)))
    if len(data) > limit:
        rows.append('... %d more bytes' % (len(data) - limit))
    return '\n'.join(rows)


def view_text(entry, data):
    if entry['type'] == S.TYPE_BAS:
        return basictok.detokenize_program(data)
    if entry['type'] == S.TYPE_TXT:
        return data.decode('latin-1', 'replace').replace('\r', '\n')
    return hexdump(data)


def exec_text(entry):
    """Exec address for display: 0 in a BIN file means 'start at the load address'."""
    if entry['exec']:
        return '$%04X' % entry['exec']
    return '= load' if entry['type'] == S.TYPE_BIN else '-'


def describe(entry):
    return '%-12s %s  %6d  $%04X  %s' % (entry['name'], TYPE_LABEL.get(entry['type'], '???'), entry['size'], entry['load'], exec_text(entry))
