"""TMS9918 VDP - behaves like the vrEmuTms9918 copy in the P65 firmware
(src/tms9918.c), including its quirks, so programs look the same as on the
device. Rendering of a whole frame is vectorised with numpy.

Firmware-specific behaviour reproduced here:
  * the mode is recomputed only when a register is written
  * the address latch is not reset by data/status accesses
  * text mode: 8 px left border, 40x6 px, 8 px right border, no sprites
  * sprites: later sprites overwrite earlier ones (the real chip does the
    opposite), 16x16 patterns do not mask the low 2 name bits, with
    magnification the line just above the sprite shows pattern row 0
    (C integer division of -1 / 2)
  * status: INT set once per frame, 5S / COL / 5th-sprite number latched
    until the CPU reads $8003 (read clears it)
"""

import numpy as np

STATUS_INT = 0x80
STATUS_5S = 0x40
STATUS_COL = 0x20

MODE_GFX1, MODE_GFX2, MODE_MULTI, MODE_TEXT = 0, 1, 2, 3
MODE_NAMES = {MODE_GFX1: 'Graphics I', MODE_GFX2: 'Graphics II',
              MODE_MULTI: 'Multicolor', MODE_TEXT: 'Text 40x24'}

_ROWS8 = np.arange(8)
_AR768 = np.arange(768)
_AR960 = np.arange(960)
_TILE_ROW_THIRD = ((np.arange(24) >> 3) << 11)
_MC_ROW = ((np.arange(24) % 4) * 2)


class TMS9918:
    def __init__(self):
        self.vram = bytearray(0x4000)
        self.vr = np.frombuffer(self.vram, dtype=np.uint8)   # shared view
        self.reset()

    def reset(self):
        self.vram[:] = b'\xff' * 0x4000
        self.regs = [0] * 8
        self.addr = 0
        self.latch = 0
        self.status = 0
        self.mode = MODE_GFX1
        self.dirty = True
        # sprite results of the last rendered frame (re-applied while the
        # picture does not change, so the flags keep being produced)
        self._spr_col = False
        self._spr_fifth = None
        self._spr_last = None
        self.frame = np.zeros((192, 256), dtype=np.uint8)

    # ------------------------------------------------------------ CPU port
    def write_addr(self, v):                 # $8003 write
        if self.latch:
            if v & 0x80:
                self.regs[v & 7] = self.addr & 0xFF
                self._update_mode()
            else:
                self.addr |= (v & 0x3F) << 8
            self.latch = 0
        else:
            self.addr = v
            self.latch = 1
        self.dirty = True

    def write_data(self, v):                 # $8002 write
        self.vram[self.addr & 0x3FFF] = v
        self.addr = (self.addr + 1) & 0x3FFF
        self.dirty = True

    def read_data(self):                     # $8002 read
        v = self.vram[self.addr & 0x3FFF]
        self.addr = (self.addr + 1) & 0x3FFF
        return v

    def read_status(self):                   # $8003 read (clears it)
        s = self.status
        self.status = 0
        return s

    def _update_mode(self):
        r = self.regs
        if r[0] & 0x02:
            self.mode = MODE_GFX2
        else:
            self.mode = {0: MODE_GFX1, 1: MODE_MULTI, 2: MODE_TEXT}.get(
                (r[1] & 0x18) >> 3, MODE_GFX1)

    def irq_pending(self):
        """State of the INT output: frame flag AND interrupt enable (R1 bit 5).
        It stays set until the CPU reads the status register."""
        return bool(self.status & STATUS_INT and self.regs[1] & 0x20)

    def display_enabled(self):
        return bool(self.regs[1] & 0x40)

    # ------------------------------------------------------------ frame
    def end_frame(self):
        """Called once per 60 Hz frame. Renders if anything changed and
        updates the status register like the firmware's renderer does."""
        rendered = False
        if self.dirty:
            self._render()
            self.dirty = False
            rendered = True
        s = self.status | STATUS_INT
        if self._spr_col:
            s |= STATUS_COL
        fifth = self._spr_fifth
        if fifth is not None:
            if not (s & STATUS_5S):
                s = (s & 0xE0) | STATUS_5S | fifth
        elif self._spr_last is not None and not (s & STATUS_5S):
            s = (s & 0xE0) | self._spr_last
        self.status = s
        return rendered

    # ---- helpers for the VDP viewer (never touch status flags) ----------
    def background(self):
        """Backdrop + tiles only (no sprites), 192x256 colour indexes."""
        r, vr = self.regs, self.vr
        if not (r[1] & 0x40):
            return np.ones((192, 256), dtype=np.uint8)
        bgc = r[7] & 0x0F
        if self.mode == MODE_TEXT:
            return self._text(vr, r, bgc)
        if self.mode == MODE_GFX1:
            return self._gfx1(vr, r, bgc)
        if self.mode == MODE_GFX2:
            return self._gfx2(vr, r, bgc)
        return self._multicolor(vr, r, bgc)

    def sprite_layer(self):
        """Sprites only: 0 = transparent, otherwise a colour index 1..15."""
        img = np.zeros((192, 256), dtype=np.uint8)
        if self.regs[1] & 0x40 and self.mode != MODE_TEXT:
            saved = (self._spr_col, self._spr_fifth, self._spr_last)
            self._sprites(self.vr, self.regs, img)
            self._spr_col, self._spr_fifth, self._spr_last = saved
        return img

    def table_layout(self):
        """[(name, start, size)] of the tables in VRAM for the current mode."""
        r = self.regs
        m = self.mode
        t = [('Name table', (r[2] & 0x0F) << 10, 960 if m == MODE_TEXT else 768)]
        if m == MODE_GFX2:
            t.append(('Colour table', (r[3] & 0x80) << 6, 6144))
            t.append(('Pattern table', (r[4] & 0x04) << 11, 6144))
        else:
            if m == MODE_GFX1:
                t.append(('Colour table', r[3] << 6, 32))
            t.append(('Pattern table', (r[4] & 0x07) << 11, 2048))
        if m != MODE_TEXT:
            t.append(('Sprite attributes', (r[5] & 0x7F) << 7, 128))
            t.append(('Sprite patterns', (r[6] & 0x07) << 11, 2048))
        return [(n, a & 0x3FFF, s) for n, a, s in t]

    def render_now(self):
        """Redraw the picture without touching the status register."""
        self._render()
        self.dirty = False

    def _render(self):
        r = self.regs
        vr = self.vr
        self._spr_col = False
        self._spr_fifth = None
        self._spr_last = None
        if not (r[1] & 0x40):
            self.frame[:] = 1                       # blanked -> black
            return
        bgc = r[7] & 0x0F
        mode = self.mode
        if mode == MODE_TEXT:
            self.frame[:] = self._text(vr, r, bgc)
            return
        if mode == MODE_GFX1:
            img = self._gfx1(vr, r, bgc)
        elif mode == MODE_GFX2:
            img = self._gfx2(vr, r, bgc)
        else:
            img = self._multicolor(vr, r, bgc)
        self._sprites(vr, r, img)
        self.frame[:] = img

    @staticmethod
    def _tiles(fg, bg, pat):
        """fg/bg: (..., 24, 32[,8]) colour planes, pat: (24, 32, 8) bytes."""
        bits = np.unpackbits(pat[..., None], axis=-1).astype(bool)   # 24,32,8,8
        px = np.where(bits, fg[..., None], bg[..., None])
        return px.transpose(0, 2, 1, 3).reshape(192, 256)

    def _gfx1(self, vr, r, bgc):
        nt = (r[2] & 0x0F) << 10
        ct = r[3] << 6
        pt = (r[4] & 0x07) << 11
        names = vr[(nt + _AR768) & 0x3FFF].astype(np.int32).reshape(24, 32)
        pat = vr[(pt + names[:, :, None] * 8 + _ROWS8) & 0x3FFF]
        col = vr[(ct + (names >> 3)) & 0x3FFF]
        fg = col >> 4
        bg = col & 0x0F
        fg = np.where(fg == 0, bgc, fg).astype(np.uint8)
        bg = np.where(bg == 0, bgc, bg).astype(np.uint8)
        return self._tiles(fg[:, :, None], bg[:, :, None], pat)

    def _gfx2(self, vr, r, bgc):
        nt = (r[2] & 0x0F) << 10
        pt = (r[4] & 0x04) << 11
        ct = (r[3] & 0x80) << 6
        names = vr[(nt + _AR768) & 0x3FFF].astype(np.int32).reshape(24, 32)
        invalid = (r[4] & 0x03) != 0x03 or (r[3] & 0x7F) != 0x7F
        if invalid:
            names &= 0x07
            third = 0
        else:
            third = _TILE_ROW_THIRD[:, None, None]
        off = third + names[:, :, None] * 8 + _ROWS8
        pat = vr[(pt + off) & 0x3FFF]
        col = vr[(ct + off) & 0x3FFF]
        fg = col >> 4
        bg = col & 0x0F
        fg = np.where(fg == 0, bgc, fg).astype(np.uint8)
        bg = np.where(bg == 0, bgc, bg).astype(np.uint8)
        return self._tiles(fg, bg, pat)

    def _text(self, vr, r, bgc):
        nt = (r[2] & 0x0F) << 10
        pt = (r[4] & 0x07) << 11
        fgc = r[7] >> 4
        if fgc == 0:
            fgc = bgc
        names = vr[(nt + _AR960) & 0x3FFF].astype(np.int32).reshape(24, 40)
        pat = vr[(pt + names[:, :, None] * 8 + _ROWS8) & 0x3FFF]
        bits = np.unpackbits(pat[..., None], axis=-1)[..., :6].astype(bool)
        mid = np.where(bits, np.uint8(fgc), np.uint8(bgc))
        img = np.full((192, 256), bgc, dtype=np.uint8)
        img[:, 8:248] = mid.transpose(0, 2, 1, 3).reshape(192, 240)
        return img

    def _multicolor(self, vr, r, bgc):
        nt = (r[2] & 0x0F) << 10
        pt = (r[4] & 0x07) << 11
        names = vr[(nt + _AR768) & 0x3FFF].astype(np.int32).reshape(24, 32)
        addr = pt + names[:, None, :] * 8 + _MC_ROW[:, None, None] + \
            np.arange(2)[None, :, None]                         # 24,2,32
        cb = vr[addr & 0x3FFF]
        fg = cb >> 4
        bg = cb & 0x0F
        fg = np.where(fg == 0, bgc, fg).astype(np.uint8)
        bg = np.where(bg == 0, bgc, bg).astype(np.uint8)
        blk = np.concatenate([np.repeat(fg[..., None], 4, -1),
                              np.repeat(bg[..., None], 4, -1)], -1)  # 24,2,32,8
        return np.repeat(blk[:, :, None], 4, axis=2).reshape(192, 256)

    def _sprites(self, vr, r, img):
        size = 16 if r[1] & 0x02 else 8
        mag = 2 if r[1] & 0x01 else 1
        span = size * mag
        sat = (r[5] & 0x7F) << 7
        spt = (r[6] & 0x07) << 11
        vram = self.vram
        count = np.zeros(192, dtype=np.int32)
        occ = np.zeros((192, 256), dtype=bool)
        fifth_line = 999
        for i in range(32):
            base = sat + i * 4
            vpos = vram[base & 0x3FFF]
            if vpos == 0xD0:
                self._spr_last = i
                break
            if vpos > 224:
                vpos -= 256
            vpos += 1
            # lines whose (C-truncated) pattern row is inside the sprite
            y0 = max(vpos - (1 if mag == 2 else 0), 0)
            y1 = min(vpos + span, 192)
            if y0 >= y1:
                continue
            ys = np.arange(y0, y1)
            rel = ys - vpos
            prow = np.where(rel < 0, -((-rel) // mag), rel // mag)
            cnt = count[y0:y1]
            over = cnt == 4
            if over.any():
                line = y0 + int(np.argmax(over))
                if line < fifth_line:
                    fifth_line = line
                    self._spr_fifth = i
            show = cnt < 4
            count[y0:y1] += 1
            if not show.any():
                continue
            attr3 = vram[(base + 3) & 0x3FFF]
            color = attr3 & 0x0F
            hpos = vram[(base + 1) & 0x3FFF] - (32 if attr3 & 0x80 else 0)
            name = vram[(base + 2) & 0x3FFF]
            prow_addr = spt + name * 8 + prow
            left = vr[prow_addr & 0x3FFF]
            if size == 16:
                right = vr[(prow_addr + 16) & 0x3FFF]
                bits = np.unpackbits(np.stack([left, right], 1), axis=1)
            else:
                bits = np.unpackbits(left[:, None], axis=1)
            if mag == 2:
                bits = np.repeat(bits, 2, axis=1)
            bits = bits.astype(bool) & show[:, None]
            x0 = max(hpos, 0)
            x1 = min(hpos + span, 256)
            if x0 >= x1:
                continue
            bits = bits[:, x0 - hpos:x1 - hpos]
            o = occ[y0:y1, x0:x1]
            if not self._spr_col and (o & bits).any():
                self._spr_col = True
            o |= bits
            if color:
                img[y0:y1, x0:x1][bits] = color

    # ------------------------------------------------------------ state
    def state(self):
        return {'vram': bytes(self.vram), 'regs': list(self.regs),
                'addr': self.addr, 'latch': self.latch, 'status': self.status}

    def set_state(self, s):
        self.vram[:] = s['vram']
        self.regs = list(s['regs'])
        self.addr = s['addr']
        self.latch = s['latch']
        self.status = s['status']
        self._update_mode()
        self.dirty = True
