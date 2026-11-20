"""VDP Viewer: look inside the TMS9918 - pattern tiles, background / sprite
layers, the sprite table and the VRAM map. Read-only; never changes VDP state."""

import numpy as np
from PySide6.QtCore import Qt, Signal, QRect, QSize
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QScrollArea, QTabWidget, QVBoxLayout, QWidget,
                               QRadioButton, QButtonGroup)

from tms9918 import MODE_GFX1, MODE_GFX2, MODE_MULTI, MODE_TEXT, MODE_NAMES

GAP = 0xFF23252B
CHECK_A, CHECK_B = 0xFF44464E, 0xFF33353B
REGION_COLORS = {
    'Name table': (90, 160, 255), 'Colour table': (255, 170, 60),
    'Pattern table': (90, 220, 120), 'Sprite attributes': (255, 90, 120),
    'Sprite patterns': (200, 110, 255),
}


def _mono(size=9):
    f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    f.setPointSize(size)
    return f


def tiles_to_image(tiles, cols):
    """tiles: (N, h, w) uint32 -> one image, `cols` tiles per row, 1 px gaps."""
    n, h, w = tiles.shape
    rows = -(-n // cols)
    t = np.full((rows * cols, h + 1, w + 1), GAP, dtype=np.uint32)
    t[:n, :h, :w] = tiles
    return t.reshape(rows, cols, h + 1, w + 1).transpose(0, 2, 1, 3).reshape(
        rows * (h + 1), cols * (w + 1))


def checker(h, w, cell=4):
    y, x = np.mgrid[0:h, 0:w]
    return np.where(((x // cell) + (y // cell)) & 1, CHECK_A, CHECK_B).astype(np.uint32)


class PixelView(QWidget):
    """Shows a uint32 ARGB image at an integer zoom. If cell size is given,
    hovering reports the cell index (for tiles)."""
    hovered = Signal(int)

    def __init__(self):
        super().__init__()
        self.arr = np.zeros((1, 1), dtype=np.uint32)
        self.image = None
        self.zoom = 2
        self.cell = None            # (cell_w, cell_h, cols, count)
        self.overlay = None         # callable(painter, zoom)
        self.setMouseTracking(True)

    def set_image(self, arr, cell=None):
        self.arr = np.ascontiguousarray(arr, dtype=np.uint32)
        h, w = self.arr.shape
        self.image = QImage(self.arr.data, w, h, w * 4, QImage.Format.Format_RGB32)
        self.cell = cell
        self._resize()
        self.update()

    def set_zoom(self, z):
        self.zoom = z
        self._resize()
        self.update()

    def _resize(self):
        h, w = self.arr.shape
        self.setFixedSize(w * self.zoom, h * self.zoom)

    def paintEvent(self, ev):
        p = QPainter(self)
        if self.image is not None:
            p.drawImage(QRect(0, 0, self.width(), self.height()), self.image)
        if self.overlay:
            self.overlay(p, self.zoom)
        p.end()

    def mouseMoveEvent(self, ev):
        idx = -1
        if self.cell:
            cw, ch, cols, count = self.cell
            x = int(ev.position().x()) // (cw * self.zoom)
            y = int(ev.position().y()) // (ch * self.zoom)
            if 0 <= x < cols:
                i = y * cols + x
                if 0 <= i < count:
                    idx = i
        self.hovered.emit(idx)

    def leaveEvent(self, ev):
        self.hovered.emit(-1)


def _zoom_combo(view, values=(1, 2, 3, 4, 6, 8), default=3):
    c = QComboBox()
    for v in values:
        c.addItem('%dx' % v, v)
    c.setCurrentIndex(values.index(default))
    c.currentIndexChanged.connect(lambda: view.set_zoom(c.currentData()))
    view.set_zoom(default)
    return c


def _scroll(widget):
    sa = QScrollArea()
    sa.setWidget(widget)
    sa.setWidgetResizable(False)
    sa.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    return sa


# ============================================================== patterns tab
class PatternsTab(QWidget):
    def __init__(self, vdp, get_pal):
        super().__init__()
        self.vdp = vdp
        self.get_pal = get_pal
        self.tiles_meta = None
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.colour = QCheckBox('Use colour table')
        self.colour.setChecked(True)
        self.view = PixelView()
        self.zoom = _zoom_combo(self.view, default=3)
        row.addWidget(self.colour)
        row.addWidget(QLabel('Zoom'))
        row.addWidget(self.zoom)
        row.addStretch(1)
        lay.addLayout(row)
        self.info = QLabel(' ')
        self.info.setFont(_mono())
        lay.addWidget(self.info)
        lay.addWidget(_scroll(self.view), 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.colour.toggled.connect(self.refresh)
        self.view.hovered.connect(self._hover)

    def refresh(self):
        v = self.vdp
        r = v.regs
        vr = v.vr
        pal = self.get_pal()
        bgc = r[7] & 0x0F
        mode = v.mode
        colour = self.colour.isChecked()
        n = 256
        cols = 16
        w = 8
        raw = None
        if mode == MODE_GFX2:
            n, cols = 768, 32
            pt, ct = (r[4] & 0x04) << 11, (r[3] & 0x80) << 6
            off = np.arange(n)[:, None] * 8 + np.arange(8)
            pat = vr[(pt + off) & 0x3FFF]
            cb = vr[(ct + off) & 0x3FFF]
            fg, bg = cb >> 4, cb & 0x0F
            self.tiles_meta = ('gfx2', pt, ct)
            self.note.setText('Graphics II: 3 pages x 256 tiles; every pixel row has its own colours.')
        elif mode == MODE_TEXT:
            pt = (r[4] & 0x07) << 11
            off = np.arange(n)[:, None] * 8 + np.arange(8)
            pat = vr[(pt + off) & 0x3FFF]
            f = r[7] >> 4
            fg = np.full((n, 8), f if f else bgc)
            bg = np.full((n, 8), bgc)
            w = 6
            self.tiles_meta = ('text', pt, 0)
            self.note.setText('Text mode: 6 pixel wide characters, colours from register 7.')
        elif mode == MODE_MULTI:
            pt = (r[4] & 0x07) << 11
            off = np.arange(n)[:, None] * 8 + np.arange(8)
            raw = vr[(pt + off) & 0x3FFF]
            self.tiles_meta = ('multi', pt, 0)
            self.note.setText('Multicolor: each pattern byte = two 4x4 colour blocks '
                              '(left / right nibble). Shown as raw bytes, one row per byte.')
        else:
            pt, ct = (r[4] & 0x07) << 11, r[3] << 6
            off = np.arange(n)[:, None] * 8 + np.arange(8)
            pat = vr[(pt + off) & 0x3FFF]
            cb = vr[(ct + (np.arange(n) >> 3)) & 0x3FFF]
            fg = np.repeat((cb >> 4)[:, None], 8, 1)
            bg = np.repeat((cb & 0x0F)[:, None], 8, 1)
            self.tiles_meta = ('gfx1', pt, ct)
            self.note.setText('Graphics I: one colour pair per group of 8 tiles.')
        if raw is not None:
            hi, lo = raw >> 4, raw & 0x0F
            hi = np.where(hi == 0, bgc, hi)
            lo = np.where(lo == 0, bgc, lo)
            px = np.concatenate([np.repeat(pal[hi][..., None], 4, -1),
                                 np.repeat(pal[lo][..., None], 4, -1)], -1)
            tiles = px.astype(np.uint32)
        else:
            bits = np.unpackbits(pat[..., None], axis=-1)[..., :w].astype(bool)
            if colour:
                fgi = np.where(fg == 0, bgc, fg)
                bgi = np.where(bg == 0, bgc, bg)
            else:
                fgi = np.full(fg.shape, 15)
                bgi = np.full(bg.shape, 1)
            tiles = np.where(bits, pal[fgi][..., None], pal[bgi][..., None]).astype(np.uint32)
        self.view.set_image(tiles_to_image(tiles, cols), (w + 1, 9, cols, n))

    def _hover(self, i):
        if i < 0 or not self.tiles_meta:
            self.info.setText(' ')
            return
        kind, pt, ct = self.tiles_meta
        s = 'Tile $%03X (%d)   pattern @ $%04X' % (i, i, (pt + i * 8) & 0x3FFF)
        if kind == 'gfx2':
            s += '   colour @ $%04X' % ((ct + i * 8) & 0x3FFF)
        elif kind == 'gfx1':
            s += '   colour @ $%04X' % ((ct + (i >> 3)) & 0x3FFF)
        self.info.setText(s)


# ============================================================== layers tab
class LayersTab(QWidget):
    def __init__(self, vdp, get_pal):
        super().__init__()
        self.vdp = vdp
        self.get_pal = get_pal
        self.boxes = []
        self.view = PixelView()
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.group = QButtonGroup(self)
        self.btns = {}
        for key, text in (('final', 'Final picture'), ('bg', 'Background only'),
                          ('spr', 'Sprites only')):
            b = QRadioButton(text)
            self.group.addButton(b)
            self.btns[key] = b
            row.addWidget(b)
            b.toggled.connect(self.refresh)
        self.btns['final'].setChecked(True)
        self.outlines = QCheckBox('Sprite outlines')
        self.outlines.setChecked(True)
        self.outlines.toggled.connect(self.refresh)
        row.addWidget(self.outlines)
        self.zoom = _zoom_combo(self.view, values=(1, 2, 3), default=2)
        row.addWidget(QLabel('Zoom'))
        row.addWidget(self.zoom)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_scroll(self.view), 1)
        self.view.overlay = self._overlay

    def sprite_boxes(self):
        """[(index, x, y, size_px)] of the active sprites (on-screen coords)."""
        v = self.vdp
        r = v.regs
        if v.mode == MODE_TEXT or not (r[1] & 0x40):
            return []
        size = 16 if r[1] & 0x02 else 8
        span = size * (2 if r[1] & 0x01 else 1)
        sat = (r[5] & 0x7F) << 7
        out = []
        for i in range(32):
            b = (sat + i * 4) & 0x3FFF
            y = v.vram[b]
            if y == 0xD0:
                break
            if y > 224:
                y -= 256
            x = v.vram[(b + 1) & 0x3FFF] - (32 if v.vram[(b + 3) & 0x3FFF] & 0x80 else 0)
            out.append((i, x, y + 1, span))
        return out

    def _overlay(self, p, z):
        if not self.outlines.isChecked() or self.btns['bg'].isChecked():
            return
        f = QFont(p.font())
        f.setPixelSize(max(9, 3 * z + 3))
        p.setFont(f)
        for i, x, y, s in self.boxes:
            col = QColor.fromHsv((i * 47) % 360, 200, 255)
            p.setPen(QPen(col, 1))
            p.drawRect(x * z, y * z, s * z - 1, s * z - 1)
            p.drawText(x * z + 2, y * z + f.pixelSize() + 1, str(i))

    def refresh(self):
        v = self.vdp
        pal = self.get_pal()
        bg = v.background()
        spr = v.sprite_layer()
        if self.btns['bg'].isChecked():
            img = pal[bg]
        elif self.btns['spr'].isChecked():
            img = np.where(spr > 0, pal[spr], checker(192, 256)).astype(np.uint32)
        else:
            img = pal[np.where(spr > 0, spr, bg)]
        self.boxes = self.sprite_boxes()
        self.view.set_image(img)


# ============================================================== sprites tab
class SpriteGrid(QWidget):
    CW, CH, COLS, ZOOM = 168, 90, 3, 4

    def __init__(self, vdp, get_pal):
        super().__init__()
        self.vdp = vdp
        self.get_pal = get_pal
        self.cells = []
        self.setFixedSize(self.CW * self.COLS, self.CH * 11)

    def refresh(self):
        v = self.vdp
        r = v.regs
        vr = v.vr
        pal = self.get_pal()
        size = 16 if r[1] & 0x02 else 8
        mag = 2 if r[1] & 0x01 else 1
        sat = (r[5] & 0x7F) << 7
        spt = (r[6] & 0x07) << 11
        self.cells = []
        active = True
        for i in range(32):
            b = (sat + i * 4) & 0x3FFF
            y, x, name, attr = (int(vr[(b + k) & 0x3FFF]) for k in range(4))
            if y == 0xD0:
                active = False
            color = attr & 0x0F
            rows = np.arange(size)
            left = vr[(spt + name * 8 + rows) & 0x3FFF]
            if size == 16:
                right = vr[(spt + name * 8 + rows + 16) & 0x3FFF]
                bits = np.unpackbits(np.stack([left, right], 1), axis=1)
            else:
                bits = np.unpackbits(left[:, None], axis=1)
            bg = checker(size, size, 2)
            px = np.where(bits.astype(bool), pal[color] if color else CHECK_A, bg).astype(np.uint32)
            arr = np.ascontiguousarray(px)
            img = QImage(arr.data, size, size, size * 4, QImage.Format.Format_RGB32).copy()
            self.cells.append((i, y, x, name, attr, color, active, img, size, mag))
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        f = _mono(8)
        p.setFont(f)
        for k, (i, y, x, name, attr, color, active, img, size, mag) in enumerate(self.cells):
            cx = (k % self.COLS) * self.CW
            cy = (k // self.COLS) * self.CH
            p.setOpacity(1.0 if active else 0.35)
            z = 64 // size
            p.drawImage(QRect(cx + 4, cy + 4, size * z, size * z), img)
            p.setPen(QPen(QColor(120, 120, 130), 1))
            p.drawRect(cx + 3, cy + 3, size * z + 1, size * z + 1)
            p.setPen(QColor(225, 225, 230))
            tx = cx + 74
            p.drawText(tx, cy + 16, '#%02d%s' % (i, '' if active else ' (off)'))
            p.drawText(tx, cy + 30, 'Y=%3d X=%3d' % (y, x))
            p.drawText(tx, cy + 44, 'name $%02X' % name)
            p.drawText(tx, cy + 58, 'col %-2d %s' % (color, 'EC' if attr & 0x80 else ''))
            p.drawText(tx, cy + 72, '%dx%d%s' % (size, size, ' x2' if mag == 2 else ''))
        p.end()


class SpritesTab(QWidget):
    def __init__(self, vdp, get_pal):
        super().__init__()
        self.vdp = vdp
        self.get_pal = get_pal
        lay = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setFont(_mono())
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        self.grid = SpriteGrid(vdp, get_pal)
        il.addWidget(self.grid)
        il.addWidget(QLabel('Sprite pattern table (white on dark, 8x8 patterns):'))
        self.pats = PixelView()
        self.pats.set_zoom(2)
        self.info = QLabel(' ')
        self.info.setFont(_mono())
        il.addWidget(self.info)
        il.addWidget(self.pats)
        il.addStretch(1)
        self.pats.hovered.connect(self._hover)
        sa = QScrollArea()                            # resizable: the column of grid + pattern table always gets its full height
        sa.setWidgetResizable(True)
        sa.setWidget(inner)
        lay.addWidget(sa, 1)

    def refresh(self):
        v = self.vdp
        r = v.regs
        if v.mode == MODE_TEXT:
            self.head.setText('Text mode has no sprites. (Tables below show the raw VRAM contents.)')
        else:
            size = 16 if r[1] & 0x02 else 8
            self.head.setText('Sprite size %dx%d, magnification x%d, attribute table @ $%04X, '
                              'pattern table @ $%04X' %
                              (size, size, 2 if r[1] & 1 else 1,
                               (r[5] & 0x7F) << 7, (r[6] & 7) << 11))
        self.grid.refresh()
        spt = (r[6] & 0x07) << 11
        off = np.arange(256)[:, None] * 8 + np.arange(8)
        bits = np.unpackbits(v.vr[(spt + off) & 0x3FFF][..., None], axis=-1).astype(bool)
        px = np.where(bits, np.uint32(0xFFE8E8EC), np.uint32(0xFF111114))
        self.spt = spt
        self.pats.set_image(tiles_to_image(px, 16), (9, 9, 16, 256))

    def _hover(self, i):
        self.info.setText(' ' if i < 0 else
                          'Sprite pattern $%02X @ $%04X' % (i, (self.spt + i * 8) & 0x3FFF))


# ============================================================== VRAM map tab
class PaletteBar(QWidget):
    def __init__(self, get_pal):
        super().__init__()
        self.get_pal = get_pal
        self.setFixedHeight(44)
        self.setMinimumWidth(16 * 26)

    def paintEvent(self, ev):
        p = QPainter(self)
        pal = self.get_pal()
        p.setFont(_mono(7))
        for i in range(16):
            col = QColor.fromRgb(int(pal[i]) & 0xFFFFFF)
            r = QRect(i * 26, 0, 24, 28)
            p.fillRect(r, col)
            p.setPen(QPen(QColor(110, 110, 120), 1))
            p.drawRect(r)
            p.setPen(QColor(210, 210, 215))
            p.drawText(QRect(i * 26, 29, 24, 14), Qt.AlignmentFlag.AlignHCenter, '%X' % i)
        p.end()


class MapTab(QWidget):
    def __init__(self, vdp, get_pal):
        super().__init__()
        self.vdp = vdp
        self.get_pal = get_pal
        lay = QVBoxLayout(self)
        self.regs = QLabel()
        self.regs.setFont(_mono())
        self.regs.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.regs)
        lay.addWidget(QLabel('Palette (index 0 = transparent / backdrop):'))
        lay.addWidget(PaletteBar(get_pal))
        row = QHBoxLayout()
        row.addWidget(QLabel('VRAM, 1 pixel = 1 byte, brightness = value, 128 bytes per row:'))
        row.addStretch(1)
        lay.addLayout(row)
        self.info = QLabel(' ')
        self.info.setFont(_mono())
        lay.addWidget(self.info)
        self.view = PixelView()
        self.view.set_zoom(3)
        self.view.hovered.connect(self._hover)
        lay.addWidget(_scroll(self.view), 1)

    def refresh(self):
        v = self.vdp
        r = v.regs
        layout = v.table_layout()
        lines = ['<b>%s</b> &nbsp; display %s &nbsp; INT %s &nbsp; R7 fg=%X bd=%X<br>' % (
            MODE_NAMES[v.mode], 'on' if r[1] & 0x40 else 'blanked',
            'enabled' if r[1] & 0x20 else 'off', r[7] >> 4, r[7] & 15)]
        owner = np.full(0x4000, -1, dtype=np.int16)
        tint = np.zeros((0x4000, 3), dtype=np.float32) + 110
        for k, (name, a, s) in enumerate(layout):
            col = REGION_COLORS[name]
            end = a + s
            idx = np.arange(a, end) & 0x3FFF
            owner[idx] = k
            tint[idx] = col
            lines.append('<span style="color:rgb(%d,%d,%d)">&#9632;</span> %-18s $%04X-$%04X (%d bytes)<br>' %
                         (col + (name.replace(' ', '&nbsp;'), a, (end - 1) & 0x3FFF, s)))
        self.regs.setText(''.join(lines))
        self.owner, self.layout = owner, layout
        val = v.vr.astype(np.float32)
        bright = 0.25 + 0.75 * (val / 255.0)
        rgb = np.where((owner >= 0)[:, None], tint, 110.0) * bright[:, None]
        rgb = np.clip(rgb, 0, 255).astype(np.uint32)
        img = 0xFF000000 | (rgb[:, 0] << 16) | (rgb[:, 1] << 8) | rgb[:, 2]
        self.view.set_image(img.reshape(128, 128), (1, 1, 128, 0x4000))

    def _hover(self, i):
        if i < 0:
            self.info.setText(' ')
            return
        # image is 128 wide x 128 tall: row-major, one byte per pixel
        t = ''
        k = self.owner[i]
        if k >= 0:
            t = '   [%s]' % self.layout[k][0]
        self.info.setText('$%04X = $%02X%s' % (i, self.vdp.vram[i], t))


# ============================================================== container
class VdpViewer(QWidget):
    def __init__(self, vdp, get_pal):
        super().__init__()
        self.tabs = QTabWidget()
        self.layers = LayersTab(vdp, get_pal)
        self.patterns = PatternsTab(vdp, get_pal)
        self.sprites = SpritesTab(vdp, get_pal)
        self.map = MapTab(vdp, get_pal)
        self.tabs.addTab(self.layers, 'Layers')
        self.tabs.addTab(self.patterns, 'Patterns')
        self.tabs.addTab(self.sprites, 'Sprites')
        self.tabs.addTab(self.map, 'VRAM map')
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.tabs)

    def refresh(self):
        w = self.tabs.currentWidget()
        if hasattr(w, 'refresh'):
            w.refresh()

    def sizeHint(self):
        return QSize(560, 700)
