"""The Python storage (emulator/p65fs.py) against the firmware's C code (src/fs.c, storage_core.c,
usb_proto.c, as a shared library): the same operations must give the same results, the same
flash image BYTE FOR BYTE, the same flash operation counts and the same USB replies.

Run it whenever the C code or p65fs.py changes. Needs a C compiler (it builds tests/ref/p65store.*).
A mismatch tells you which side to fix - the C code is the reference."""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, '..', 'emulator'))     # the emulator lives next to the firmware in the P65 repo
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import p65fs as P            # noqa: E402
import storage as PY         # noqa: E402

CC = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
REF = None
if CC:
    try:
        import ctypes_store as REF
        if not os.path.exists(REF.DLL_PATH):
            REF._try_build()
        if not os.path.exists(REF.DLL_PATH):
            REF = None
    except Exception:
        REF = None

SIZE = 256 * 1024
NAMES = ['A.BAS', 'B.BIN', 'GAME.BIN', 'HELLO.TXT', 'LONGNAME.BAS', 'Z.BIN', 'DEMO1.BIN', 'X.TXT']
SIZES = [0, 1, 100, 4031, 4032, 4033, 4064, 4065, 8100, 8128, 12000, 30000, 65000]


def call(fn, *a):
    try:
        return ('ok', fn(*a))
    except Exception as e:                       # StorageError of either implementation
        return ('err', getattr(e, 'code', repr(e)))


@unittest.skipUnless(REF, 'no C compiler / reference library')
class TestPyVsC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.c_path = os.path.join(self.tmp, 'c.img')
        self.p_path = os.path.join(self.tmp, 'p.img')
        self.c = REF.Store(self.c_path, SIZE)
        self.p = PY.Store(self.p_path, SIZE)

    def tearDown(self):
        self.c.close()
        self.p.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def same_state(self, what=''):
        self.c.flush()
        if os.path.exists(self.c_path):
            with open(self.c_path, 'rb') as f:
                c_img = f.read()
        else:                                                  # the C store writes its file only once something changed
            c_img = bytes([0xFF]) * SIZE
        self.assertTrue(c_img == bytes(self.p.flash.img), 'flash images differ after ' + what)
        self.assertEqual(self.c.entries(), self.p.entries(), 'directory differs after ' + what)
        self.assertEqual((self.c.free_bytes(), self.c.total_bytes()), (self.p.free_bytes(), self.p.total_bytes()), what)

    def test_names(self):
        for text in ['game.bin', 'GAME', 'a.b', 'LONGNAME.BAS', 'TOOLONGNAME.BIN', 'x.toolong', '', '.bin', 'a b',
                     'a-b_c.t$t', 'abc.', 'a..b', 'ä.bin', "it's.bin"]:
            for ext in (None, 'BAS', 'TXT'):
                c = call(self.c.parse_name, text, ext)
                p = call(self.p.parse_name, text, ext)
                self.assertEqual(c, p, (text, ext))
        for n11 in (b'GAME    BIN', b'A       B  ', b'ABCDEFGHXYZ', b'        BIN'):
            self.assertEqual(self.c.format_name(n11), self.p.format_name(n11), n11)

    def test_random_file_operations(self):
        rnd = random.Random(1234)
        for step in range(150):
            op = rnd.choice(['write', 'write', 'write', 'delete', 'rename', 'read', 'format' if step % 60 == 59 else 'write'])
            name = rnd.choice(NAMES)
            what = '%d:%s %s' % (step, op, name)
            if op == 'write':
                data = bytes(rnd.randrange(256) for _ in range(1)) * rnd.choice(SIZES)
                data = bytes((i * 7 + step) & 255 for i in range(len(data)))
                t, load, ex = rnd.choice([0, 1, 2]), rnd.randrange(0x10000), rnd.randrange(0x10000)
                self.assertEqual(call(self.c.write, name, data, t, load, ex), call(self.p.write, name, data, t, load, ex), what)
            elif op == 'delete':
                self.assertEqual(call(self.c.delete, name), call(self.p.delete, name), what)
            elif op == 'rename':
                other = rnd.choice(NAMES)
                self.assertEqual(call(self.c.rename, name, other), call(self.p.rename, name, other), what)
            elif op == 'read':
                self.assertEqual(call(self.c.read, name), call(self.p.read, name), what)
            else:
                self.assertEqual(call(self.c.format), call(self.p.format), what)
            self.same_state(what)
        # remount both from their images: the same files, the same wear counts
        self.c.close()
        self.p.close()
        c2, p2 = REF.Store(self.c_path, SIZE), PY.Store(self.p_path, SIZE)
        self.assertEqual(c2.entries(), p2.entries())
        c2.close()
        p2.close()

    def test_disk_full_and_overwrite_in_tight_space(self):
        data = bytes(range(256)) * 400                     # ~100 KB each
        for i in range(4):
            self.assertEqual(call(self.c.write, 'F%d.BIN' % i, data, 0, 0x800, 0),
                             call(self.p.write, 'F%d.BIN' % i, data, 0, 0x800, 0))
        big = data * 2
        for name in ('F1.BIN', 'NEW.BIN', 'F1.BIN'):
            self.assertEqual(call(self.c.write, name, big, 0, 0, 0), call(self.p.write, name, big, 0, 0, 0), name)
            self.same_state(name)

    # ---- the CPU's mailbox ----------------------------------------------------------------------------
    def run_mailbox(self, regs, name=b'', name2=b'', mem_init=None):
        out = []
        for store in (self.c, self.p):
            mem = bytearray(0x10002)
            if mem_init:
                for a, d in mem_init.items():
                    mem[a:a + len(d)] = d
            R = 0x8010
            store.init_regs(mem, R)
            mem[0x0200:0x0200 + len(name)] = name
            mem[0x0240:0x0240 + len(name2)] = name2
            mem[R + P.STO_NAME_L], mem[R + P.STO_NAME_H], mem[R + P.STO_NAMELEN] = 0x00, 0x02, len(name)
            mem[R + P.STO_NAME2_L], mem[R + P.STO_NAME2_H], mem[R + P.STO_NAME2LEN] = 0x40, 0x02, len(name2)
            for k, v in regs.items():
                mem[R + k] = v
            ops = store.execute(mem, 0x7FFF, R)
            out.append((bytes(mem), ops))
        self.assertEqual(out[0][1], out[1][1], 'flash operation counts differ for %r' % regs)
        self.assertEqual(out[0][0], out[1][0], 'memory / registers differ for %r' % regs)
        return out[1][0][0x8010 + P.STO_STATUS]

    def test_mailbox_commands(self):
        prog = {0x0800: bytes(range(256)) * 20, 0x2000: b'hello world' * 500}
        run = self.run_mailbox
        # SAVE two files, LOAD, INFO, DIRENT, FREE, RENAME, DELETE, errors
        self.assertEqual(run({P.STO_CMD: P.STO_C_SAVE, P.STO_ADDR_L: 0x00, P.STO_ADDR_H: 0x08, P.STO_LEN_L: 0x00,
                              P.STO_LEN_H: 0x14, P.STO_TYPE: 0, P.STO_EXEC_L: 0x10, P.STO_EXEC_H: 0x08}, b'PROG', mem_init=prog), 0)
        self.same_state('mailbox save')
        # the C and Python stores now hold the same file; every following command sees the same state
        for cmd in (P.STO_C_LOAD, P.STO_C_INFO):
            for flags, addr in ((0, 0), (P.STO_F_ADDR, 0x3000), (P.STO_F_ADDR, 0x7F00), (P.STO_F_ADDR, 0xF000)):
                run({P.STO_CMD: cmd, P.STO_TYPE: P.STO_TYPE_ANY, P.STO_FLAGS: flags, P.STO_ADDR_L: addr & 255,
                     P.STO_ADDR_H: addr >> 8}, b'PROG')
        run({P.STO_CMD: P.STO_C_LOAD, P.STO_TYPE: P.STO_TYPE_BAS}, b'PROG')        # type mismatch
        run({P.STO_CMD: P.STO_C_LOAD, P.STO_TYPE: P.STO_TYPE_ANY}, b'NOPE')
        run({P.STO_CMD: P.STO_C_LOAD, P.STO_TYPE: P.STO_TYPE_ANY}, b'BAD NAME')
        run({P.STO_CMD: P.STO_C_LOAD, P.STO_TYPE: P.STO_TYPE_ANY}, b'')
        for i in range(3):
            run({P.STO_CMD: P.STO_C_DIRENT, P.STO_INDEX: i, P.STO_NAME_L: 0x80, P.STO_NAME_H: 0x03})
        run({P.STO_CMD: P.STO_C_DIRENT, P.STO_INDEX: 0, P.STO_NAME_L: 0xFA, P.STO_NAME_H: 0xFF})     # buffer beyond 64 KB
        run({P.STO_CMD: P.STO_C_FREE})
        run({P.STO_CMD: P.STO_C_RENAME, P.STO_TYPE: 0}, b'PROG', b'PROG2')
        run({P.STO_CMD: P.STO_C_RENAME, P.STO_TYPE: 0}, b'PROG2', b'PROG2')
        run({P.STO_CMD: P.STO_C_SAVE, P.STO_ADDR_L: 0, P.STO_ADDR_H: 0xFF, P.STO_LEN_L: 0, P.STO_LEN_H: 0x02}, b'BIG')   # beyond 64 KB
        run({P.STO_CMD: P.STO_C_FORMAT, P.STO_ADDR_L: 0, P.STO_ADDR_H: 0}, )                                         # wrong magic
        run({P.STO_CMD: P.STO_C_DELETE, P.STO_TYPE: 0}, b'PROG2')
        run({P.STO_CMD: 99})
        run({P.STO_CMD: P.STO_C_NOP})
        run({P.STO_CMD: P.STO_C_FORMAT, P.STO_ADDR_L: 0xA5, P.STO_ADDR_H: 0xF0})
        self.same_state('mailbox sequence')

    # ---- the USB protocol ---------------------------------------------------------------------------------
    @staticmethod
    def frame(cmd, data=b'', bad_crc=False):
        pl = bytes([cmd]) + data
        head = len(pl).to_bytes(2, 'little')
        crc = P.crc16(head + pl)
        if bad_crc:
            crc ^= 1
        return b'\xA5\x5A' + head + pl + crc.to_bytes(2, 'little')

    def test_usb_protocol_replies_are_identical(self):
        n = P.parse_name
        rnd = random.Random(7)
        data = bytes(rnd.randrange(256) for _ in range(9000))
        crc = P.crc32(data)

        def put(name, d, ctype=0):
            yield self.frame(P.PROTO_C_PUT_BEGIN, name + bytes([ctype]) + (0x800).to_bytes(2, 'little') + (0x800).to_bytes(2, 'little')
                             + len(d).to_bytes(4, 'little'))
            for off in range(0, len(d), 1024):
                yield self.frame(P.PROTO_C_PUT_DATA, off.to_bytes(4, 'little') + d[off:off + 1024])
            yield self.frame(P.PROTO_C_PUT_END, P.crc32(d).to_bytes(4, 'little'))

        frames = [self.frame(P.PROTO_C_HELLO), b'garbage\x00\xA5\xA5\x5A', self.frame(P.PROTO_C_LIST, b'\0\0')]
        frames += list(put(n('DATA.BIN'), data))
        frames += list(put(n('SMALL.TXT'), b'tiny', 2))
        frames += [self.frame(P.PROTO_C_LIST, b'\0\0'), self.frame(P.PROTO_C_LIST, b'\1\0'), self.frame(P.PROTO_C_LIST, b'\x09\0')]
        frames += [self.frame(P.PROTO_C_READ, n('DATA.BIN') + (0).to_bytes(4, 'little') + (1024).to_bytes(2, 'little')),
                   self.frame(P.PROTO_C_READ, n('DATA.BIN') + (8500).to_bytes(4, 'little') + (1024).to_bytes(2, 'little')),
                   self.frame(P.PROTO_C_READ, n('DATA.BIN') + (9001).to_bytes(4, 'little') + (10).to_bytes(2, 'little')),
                   self.frame(P.PROTO_C_READ, n('NOPE.BIN') + bytes(6)), self.frame(P.PROTO_C_READ, b'short')]
        # sequence error, bad end crc, no session, bad frame crc, wrong frame length, unknown command
        frames += [self.frame(P.PROTO_C_PUT_DATA, bytes(4) + b'x'),
                   self.frame(P.PROTO_C_PUT_BEGIN, n('BAD.BIN') + bytes([0]) + bytes(4) + (10).to_bytes(4, 'little')),
                   self.frame(P.PROTO_C_PUT_DATA, (5).to_bytes(4, 'little') + b'xxxxx'),
                   self.frame(P.PROTO_C_PUT_DATA, bytes(4) + b'0123456789'),
                   self.frame(P.PROTO_C_PUT_END, (123).to_bytes(4, 'little')),
                   self.frame(P.PROTO_C_PUT_END, bytes(4)),
                   self.frame(P.PROTO_C_HELLO, bad_crc=True), b'\xA5\x5A\xFF\xFF', self.frame(0x7F)]
        frames += [self.frame(P.PROTO_C_RENAME, n('SMALL.TXT') + n('NEW.TXT')), self.frame(P.PROTO_C_RENAME, n('NOPE.TXT') + n('X.TXT')),
                   self.frame(P.PROTO_C_RENAME, n('NEW.TXT') + n('DATA.BIN')),
                   self.frame(P.PROTO_C_DELETE, n('DATA.BIN')), self.frame(P.PROTO_C_DELETE, n('DATA.BIN')),
                   self.frame(P.PROTO_C_FORMAT, b'nope'), self.frame(P.PROTO_C_FORMAT, P.PROTO_FORMAT_MAGIC.to_bytes(4, 'little')),
                   self.frame(P.PROTO_C_HELLO)]
        now = 0
        for i, fr in enumerate(frames):
            now += 5
            rc, rp = self.c.protocol(fr, now), self.p.protocol(fr, now)
            self.assertEqual(rc, rp, 'reply %d differs (%r)' % (i, fr[:12]))
            self.same_state('frame %d' % i)
        # a PUT that stalls is dropped after the timeout in both
        b = self.frame(P.PROTO_C_PUT_BEGIN, n('STALL.BIN') + bytes([0]) + bytes(4) + (50).to_bytes(4, 'little'))
        self.assertEqual(self.c.protocol(b, 1000), self.p.protocol(b, 1000))
        late = self.frame(P.PROTO_C_PUT_DATA, bytes(4) + b'x' * 10)
        self.assertEqual(self.c.protocol(late, 1000 + P.PROTO_PUT_TIMEOUT_MS + 1), self.p.protocol(late, 1000 + P.PROTO_PUT_TIMEOUT_MS + 1))


class TestPowerFailure(unittest.TestCase):
    """The Python file system must also survive a power cut at every flash operation
    (the C code has its own sweep in tests/fs_test.c)."""

    @staticmethod
    def workload(fs):
        w1 = bytes(range(256)) * 40
        w2 = bytes(reversed(range(256))) * 33
        steps = [('put', b'ONE     BIN', w1), ('put', b'TWO     BIN', w2), ('put', b'ONE     BIN', w2),
                 ('del', b'TWO     BIN', None), ('ren', b'ONE     BIN', b'THREE   BIN'), ('put', b'FOUR    TXT', b'x' * 5000)]
        for kind, name, data in steps:
            if kind == 'put':
                fs.put_file(name, data, 0, 0x800, 0)
            elif kind == 'del':
                fs.delete(name)
            else:
                fs.rename(name, data)

    def test_cut_power_at_every_operation(self):
        size = 64 * 1024
        ref = P.Flash(size)
        fs = P.FileSystem(ref)
        self.workload(fs)
        total = ref.ops
        self.assertGreater(total, 30)
        known = {b'\x00' * 0}
        valid = [bytes(range(256)) * 40, bytes(reversed(range(256))) * 33, b'x' * 5000]
        for mode in ('before', 'half', 'after'):
            for cut in range(1, total + 1):
                fl = P.Flash(size)
                fl.cut_at, fl.cut_mode = cut, mode
                fs = P.FileSystem(fl)
                try:
                    self.workload(fs)
                except P.PowerCut:
                    pass
                fl.cut_at = None
                fs2 = P.FileSystem(fl)                                   # reboot
                for e in fs2.files:                                      # every visible file is complete and intact
                    got, data = fs2.read(e, 0, e.size)
                    self.assertEqual(got, e.size, (mode, cut))
                    self.assertEqual(P.crc32(data), e.crc, (mode, cut, P.format_name(e.name)))
                    self.assertIn(data, valid, (mode, cut))
                self.assertEqual(fs2.put_file(b'AFTER   BIN', b'ok' * 100, 0, 0, 0), P.FS_OK, (mode, cut))   # still usable


if __name__ == '__main__':
    unittest.main()
