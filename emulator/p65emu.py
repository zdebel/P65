#!/usr/bin/env python3
"""P65 emulator (RP2040 + 65C02 + TMS9918 on ILI9341) in Qt6.

Usage:  python p65emu.py [--rom FILE] [--type FILE.bas] [--state FILE]
"""

import argparse
import os
import sys
import time

import numpy as np
import icons
from PySide6.QtCore import Qt, QTimer, QSettings, QSize, QRect, QRectF, QPointF
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QFontDatabase,
                           QImage, QKeySequence, QPainter, QPen, QPixmap,
                           QRadialGradient, QLinearGradient, QPalette, QGuiApplication)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDockWidget, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QAbstractScrollArea, QScrollArea, QFrame, QSizePolicy, QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

from cpu65c02 import disassemble
from vdpviewer import VdpViewer
import storage as storage_mod
from storagewin import StorageDialog
from machine import (P65, CPU_CLOCK, FRAME_CYCLES, ROM_START, default_rom,
                     load_rom_h)
from tms9918 import MODE_NAMES, MODE_TEXT

APP_NAME = 'P65 Emulator'
LCD_W, LCD_H = 320, 240
# where the firmware puts the 256x192 picture on the 320x240 ILI9341
# (video_init: columns 31..286, pages 23..214)
PIC_X, PIC_Y = 31, 23

# --- palettes (index -> 0xAARRGGBB) ------------------------------------------
# the firmware's LCD palette (video.c tmspal_bgr16: byte-swapped RGB565)
_FW_PAL16 = [0x0000, 0x0000, 0x4826, 0xEF5E, 0xBD52, 0xBF7B, 0x89D2, 0x5E47,
             0xAAFA, 0xCFFB, 0x0AD6, 0x70E6, 0x8725, 0xD7CA, 0x79CE, 0xFFFF]


def _rgb565_swapped(v):
    s = ((v & 0xFF) << 8) | (v >> 8)
    r, g, b = (s >> 11) & 0x1F, (s >> 5) & 0x3F, s & 0x1F
    return 0xFF000000 | ((r * 255 // 31) << 16) | ((g * 255 // 63) << 8) | (b * 255 // 31)


PALETTES = {
    'lcd': ('LCD (as on the device)', np.array([_rgb565_swapped(v) for v in _FW_PAL16], np.uint32)),
    'tms': ('Classic TMS9918', np.array([
        0xFF000000, 0xFF000000, 0xFF21C942, 0xFF5EDC78, 0xFF5455ED, 0xFF7D75FC,
        0xFFD3524D, 0xFF43EBF6, 0xFFFD5554, 0xFFFF7978, 0xFFD3C153, 0xFFE5CE80,
        0xFF21B03C, 0xFFC95BBA, 0xFFCCCCCC, 0xFFFFFFFF], np.uint32)),
}

# --- keys -----------------------------------------------------------------------
K = Qt.Key
SPECIAL_KEYS = {
    int(K.Key_Escape): 27, int(K.Key_Backspace): 8, int(K.Key_Tab): 9,
    int(K.Key_Return): 13, int(K.Key_Enter): 13,
    int(K.Key_Left): 17, int(K.Key_Right): 18,      # P65: <-- 17, --> 18
}
KEY_A, KEY_Z = int(K.Key_A), int(K.Key_Z)
KEY_EQUAL, KEY_PLUS = int(K.Key_Equal), int(K.Key_Plus)
MOD_CTRL = Qt.KeyboardModifier.ControlModifier
MOD_ALT = Qt.KeyboardModifier.AltModifier


def mono_font(size=None):
    f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if size:
        f.setPointSize(size)
    return f


# ================================================================== widgets
class ScreenWidget(QWidget):
    """The ILI9341: 320x240, picture at (31,23), integer scaled."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.scale = 2
        self.grid = False
        self.buf = np.zeros((LCD_H, LCD_W), dtype=np.uint32)
        self.buf[:] = 0xFF000000
        self.image = QImage(self.buf.data, LCD_W, LCD_H, LCD_W * 4,
                            QImage.Format.Format_RGB32)
        self._grid_cache = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(LCD_W, LCD_H)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def sizeHint(self):
        return QSize(LCD_W * self.scale, LCD_H * self.scale)

    def set_frame(self, frame, pal, border):
        self.buf[:] = border
        self.buf[PIC_Y:PIC_Y + 192, PIC_X:PIC_X + 256] = pal[frame]
        self.update()

    def current_scale(self):
        return max(1, min(self.width() // LCD_W, self.height() // LCD_H))

    def target_rect(self):
        s = self.current_scale()
        w, h = LCD_W * s, LCD_H * s
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def _grid_pixmap(self, s):
        if self._grid_cache and self._grid_cache[0] == s:
            return self._grid_cache[1]
        pm = QPixmap(LCD_W * s, LCD_H * s)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setPen(QPen(QColor(0, 0, 0, 70), 1))
        for x in range(0, LCD_W * s, s):
            p.drawLine(x, 0, x, LCD_H * s)
        for y in range(0, LCD_H * s, s):
            p.drawLine(0, y, LCD_W * s, y)
        p.end()
        self._grid_cache = (s, pm)
        return pm

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(12, 12, 14))
        r = self.target_rect()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(r, self.image)
        s = self.current_scale()
        if self.grid and s >= 3:
            p.drawPixmap(r.topLeft(), self._grid_pixmap(s))
        # glass reflection
        g = QLinearGradient(QPointF(r.topLeft()), QPointF(r.bottomRight()))
        g.setColorAt(0.0, QColor(255, 255, 255, 10))
        g.setColorAt(0.45, QColor(255, 255, 255, 0))
        p.fillRect(r, g)
        if not self.hasFocus():
            p.setPen(QColor(255, 255, 255, 140))
            p.setFont(QFont(self.font().family(), 9))
            p.drawText(r.adjusted(0, 0, -6, -4),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                       'click to type')
        p.end()

    # keyboard goes to the emulated machine (Tab included)
    def focusNextPrevChild(self, nxt):
        return False

    def keyPressEvent(self, ev):
        if not self.main.handle_key(ev, True):
            super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if not self.main.handle_key(ev, False):
            super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev):
        self.main.machine.kbd.release_all()
        self.update()
        super().focusOutEvent(ev)

    def focusInEvent(self, ev):
        self.update()
        super().focusInEvent(ev)

    def mousePressEvent(self, ev):
        self.setFocus()


class LedBar(QWidget):
    """The three LEDs on PCAL6524 port 2 ($8000 bits 0..2)."""

    # LED0 red, LED1 yellow, LED2 green; in the case they sit left to right
    # as LED2, LED1, LED0 (see paintEvent)
    COLORS = [QColor(255, 60, 50), QColor(255, 190, 40), QColor(60, 230, 90)]

    def __init__(self):
        super().__init__()
        self.state = 0
        self.setFixedHeight(34)

    def set_state(self, v):
        if v != self.state:
            self.state = v
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(self.font())
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        x = 16
        cy = self.height() / 2
        for n in (2, 1, 0):
            on = bool(self.state & (1 << n))
            col = self.COLORS[n]
            c = QPointF(x + 7, cy)
            if on:
                glow = QRadialGradient(c, 16)
                glow.setColorAt(0, QColor(col.red(), col.green(), col.blue(), 150))
                glow.setColorAt(1, QColor(col.red(), col.green(), col.blue(), 0))
                p.setBrush(glow)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(c, 16, 16)
            body = QRadialGradient(QPointF(c.x() - 2, c.y() - 2), 9)
            if on:
                body.setColorAt(0, QColor(255, 255, 230))
                body.setColorAt(0.35, col)
                body.setColorAt(1, col.darker(160))
            else:
                body.setColorAt(0, col.darker(260))
                body.setColorAt(1, col.darker(500))
            p.setBrush(body)
            p.setPen(QPen(QColor(0, 0, 0, 160), 1))
            p.drawEllipse(c, 6.5, 6.5)
            p.setPen(QColor(200, 200, 205) if on else QColor(120, 120, 128))
            p.drawText(QRectF(x + 18, 0, 40, self.height()),
                       Qt.AlignmentFlag.AlignVCenter, 'LED%d' % n)
            x += 70
        p.end()


# P65 logo (pixel-style, from the project's SVG: 50x50 grid, foreground yellow instead of green)
LOGO_RECTS = [(4, 20, 10, 10), (20, 20, 10, 10), (22, 10, 8, 8), (36, 20, 10, 10), (38, 10, 8, 8), (42, 4, 4, 4),
              (4, 35, 3, 3), (4, 41, 3, 5), (4, 32, 10, 3), (4, 38, 10, 3), (11, 35, 3, 3),
              (20, 35, 3, 2), (20, 40, 3, 3), (20, 32, 10, 3), (20, 37, 10, 3), (20, 43, 10, 3), (27, 40, 3, 3),
              (36, 35, 3, 2), (36, 32, 10, 3), (36, 37, 10, 3), (36, 43, 10, 3), (43, 40, 3, 3)]
LOGO_FG = QColor('#ffd400')


class LogoWidget(QWidget):
    def __init__(self, size=44):
        super().__init__()
        self.setFixedSize(size, size)
        self.setToolTip('P65')

    def paintEvent(self, ev):
        p = QPainter(self)
        k = self.width() / 50.0
        p.scale(k, k)
        p.setPen(Qt.PenStyle.NoPen)
        for x, y, w, h in LOGO_RECTS:
            p.fillRect(QRectF(x, y, w, h), LOGO_FG)
        p.end()


class DeviceWidget(QWidget):
    """Case of the handheld: screen in a bezel, name plate and LEDs."""

    def __init__(self, screen, leds):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 26, 26, 14)
        lay.setSpacing(10)
        lay.addWidget(screen, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(leds, 1)
        self.title = LogoWidget(72)
        bottom.addWidget(self.title)
        lay.addLayout(bottom)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, QColor(58, 60, 68))
        g.setColorAt(1, QColor(34, 35, 41))
        p.setBrush(g)
        p.setPen(QPen(QColor(85, 88, 98), 1.2))
        p.drawRoundedRect(r, 18, 18)
        p.end()


# ================================================================== debugger
# ---- debugger colours (dark theme)
C_ADDR = '#6f8fc8'      # addresses
C_DIM = '#585b66'       # zeros, unset flags, labels
C_TXT = '#dcdce1'       # ordinary text
C_HEX = '#e8e8ee'       # non-zero bytes
C_ASCII = '#7ad08a'     # printable characters
C_IO = '#d38ad8'        # I/O page
C_ROM = '#8a8fa0'       # ROM addresses
C_CHG = '#ffb347'       # changed values
C_PC = '#ffd400'        # program counter
C_BP = '#ff5a50'        # breakpoint
C_OPER = '#6fd0d8'      # operands
MN_BRANCH = {'BRA', 'BEQ', 'BNE', 'BCC', 'BCS', 'BMI', 'BPL', 'BVC', 'BVS', 'BBR', 'BBS', 'JMP', 'JSR', 'RTS', 'RTI', 'BRK'}
MN_MEM = {'LDA', 'LDX', 'LDY', 'STA', 'STX', 'STY', 'STZ', 'PHA', 'PLA', 'PHX', 'PLX', 'PHY', 'PLY', 'PHP', 'PLP'}


def _esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _span(color, text, bold=False):
    return '<span style="color:%s%s">%s</span>' % (color, ';font-weight:bold' if bold else '', _esc(text))


class HexView(QAbstractScrollArea):
    """Virtual hex dump of the whole address space: only the visible rows are painted, so it can be
    scrolled freely and refreshing never moves the view. Changed bytes glow for a moment."""

    COLS = 16
    AGE = 12                                           # refreshes a changed byte stays highlighted

    def __init__(self):
        super().__init__()
        self.setFont(mono_font(9))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        fm = self.fontMetrics()
        self.cw = fm.horizontalAdvance('0')
        self.rh = fm.height() + 2
        self.size = 0x10000
        self.src = bytearray(self.size)
        self.age = np.zeros(self.size, dtype=np.uint8)
        self.prev = None
        self.pc = None
        self.sp = None
        self.cpu_view = True
        self.verticalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        self.setMinimumHeight(self.rh * 8)
        self._layout()

    # columns (in characters): addr(4)+2, bytes 16x3 with a gap after 8, 2, ascii 16
    def _byte_x(self, k):
        return (6 + k * 3 + (1 if k >= 8 else 0)) * self.cw

    def _layout(self):
        total = 6 + 16 * 3 + 1 + 2 + 16 + 1
        self.line_w = total * self.cw
        rows = self.size // self.COLS
        vh = self.viewport().height()
        self.visible = max(1, (vh - self.rh) // self.rh)
        self.verticalScrollBar().setRange(0, max(0, rows - self.visible))
        self.verticalScrollBar().setPageStep(self.visible)
        self.horizontalScrollBar().setRange(0, max(0, self.line_w - self.viewport().width()))
        self.horizontalScrollBar().setPageStep(self.viewport().width())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._layout()

    def set_source(self, src, size, cpu_view):
        if size != self.size or cpu_view != self.cpu_view:
            self.size = size
            self.cpu_view = cpu_view
            self.age = np.zeros(size, dtype=np.uint8)
            self.prev = None
            self._layout()
        self.src = src

    def goto(self, addr):
        self.verticalScrollBar().setValue((addr % self.size) // self.COLS)

    def top_address(self):
        return self.verticalScrollBar().value() * self.COLS

    def update_data(self):
        """Called on every debugger refresh: track changes, repaint. Never touches the scroll position."""
        cur = np.frombuffer(self.src, dtype=np.uint8, count=self.size)
        if self.prev is not None and len(self.prev) == len(cur):
            self.age[self.age > 0] -= 1
            self.age[cur != self.prev] = self.AGE
        self.prev = cur.copy()
        self.viewport().update()

    def _region_color(self, addr):
        if not self.cpu_view:
            return C_ADDR
        if addr >= 0x8100:
            return C_ROM
        if addr >= 0x8000:
            return C_IO
        return C_ADDR

    def paintEvent(self, ev):
        p = QPainter(self.viewport())
        p.setFont(self.font())
        w = self.viewport().width()
        p.fillRect(self.viewport().rect(), QColor(24, 25, 30))
        ox = -self.horizontalScrollBar().value()
        rh, cw = self.rh, self.cw
        base = self.fontMetrics().ascent() + 1
        # header
        p.fillRect(0, 0, w, rh, QColor(36, 38, 46))
        p.setPen(QColor(C_DIM))
        for k in range(16):
            p.drawText(ox + self._byte_x(k), base, '%02X' % k)
        p.drawText(ox + (6 + 48 + 1 + 2) * cw, base, 'ASCII')
        first = self.verticalScrollBar().value()
        src, age = self.src, self.age
        for r in range(self.visible + 1):
            row = first + r
            if row * self.COLS >= self.size:
                break
            a = row * self.COLS
            y = rh + r * rh
            if r % 2:
                p.fillRect(0, y, w, rh, QColor(28, 29, 35))
            p.setPen(QColor(self._region_color(a)))
            p.drawText(ox, y + base, '%04X' % a)
            asc_x = ox + (6 + 48 + 1 + 2) * cw
            for k in range(16):
                ad = a + k
                b = src[ad]
                x = ox + self._byte_x(k)
                if self.cpu_view and self.pc == ad:
                    p.fillRect(x - 1, y, cw * 2 + 2, rh, QColor(70, 62, 10))
                elif self.cpu_view and self.sp == ad:
                    p.fillRect(x - 1, y, cw * 2 + 2, rh, QColor(20, 60, 90))
                n = age[ad]
                if n:
                    t = n / float(self.AGE)
                    col = QColor(int(90 + 165 * t), int(90 + 80 * t), int(100 - 20 * t))
                elif b == 0:
                    col = QColor(C_DIM)
                else:
                    col = QColor(C_HEX)
                p.setPen(col)
                p.drawText(x, y + base, '%02X' % b)
                ch = b & 0x7F
                if 32 <= ch < 127:
                    p.setPen(QColor(C_CHG) if n else QColor(C_ASCII))
                    p.drawText(asc_x + k * cw, y + base, chr(ch))
                else:
                    p.setPen(QColor(C_DIM))
                    p.drawText(asc_x + k * cw, y + base, '·')
        p.end()


class DisView(QTextEdit):
    """Colored disassembly; double click on a line toggles a breakpoint there."""

    def __init__(self, on_toggle):
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setFont(mono_font(9))
        self.addrs = []
        self.on_toggle = on_toggle
        self._html = None

    def set_lines(self, html, addrs):
        self.addrs = addrs
        if html == self._html:                        # nothing changed: keep scroll, selection and flicker-free
            return
        self._html = html
        vs, hs = self.verticalScrollBar().value(), self.horizontalScrollBar().value()
        self.setHtml(html)
        self.verticalScrollBar().setValue(vs)
        self.horizontalScrollBar().setValue(hs)

    def mouseDoubleClickEvent(self, ev):
        blk = self.cursorForPosition(ev.position().toPoint()).blockNumber()
        if 0 <= blk < len(self.addrs):
            self.on_toggle(self.addrs[blk])


class DebugPanel(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        m = main.machine
        mono = mono_font(9)
        lay = QVBoxLayout(self)
        self._prev_regs = None
        self._prev_vdp = None

        btns = QHBoxLayout()
        self.btn_run = QPushButton('Pause')
        self.btn_run.clicked.connect(main.toggle_pause)
        b_step = QPushButton('Step')
        b_step.clicked.connect(main.step_instruction)
        b_frame = QPushButton('Frame')
        b_frame.setToolTip('Emulate 1/60 s, then stop')
        b_frame.clicked.connect(main.step_frame)
        for b in (self.btn_run, b_step, b_frame):
            btns.addWidget(b)
        lay.addLayout(btns)

        self.regs = QLabel()
        self.regs.setFont(mono)
        self.regs.setTextFormat(Qt.TextFormat.RichText)
        self.regs.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._group('CPU registers', self.regs))

        self.dis = DisView(self.toggle_bp)
        self.dis.setMinimumHeight(170)
        self.dis.setToolTip('Double click a line to toggle a breakpoint')
        lay.addWidget(self._group('Disassembly (from PC)  -  double click: breakpoint', self.dis), 2)

        memw = QWidget()
        ml = QVBoxLayout(memw)
        ml.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.mem_src = QComboBox()
        self.mem_src.addItems(['CPU memory', 'VRAM'])
        self.mem_addr = QLineEdit('0000')
        self.mem_addr.setMaximumWidth(70)
        self.mem_addr.setFont(mono)
        self.mem_src.currentIndexChanged.connect(self._source_changed)
        self.mem_addr.returnPressed.connect(self._goto_edit)
        row.addWidget(self.mem_src)
        row.addWidget(QLabel('go to $'))
        row.addWidget(self.mem_addr)
        self.jumps = {}
        for label, tip in (('PC', 'Show the program counter'), ('SP', 'Show the stack'),
                           ('ZP', 'Zero page $0000'), ('BAS', 'BASIC program area $0400'),
                           ('IO', 'I/O page $8000'), ('VEC', 'Vectors $FFF0')):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setMaximumWidth(38)
            b.clicked.connect(lambda _=False, k=label: self._jump(k))
            row.addWidget(b)
        row.addStretch(1)
        ml.addLayout(row)
        self.hexv = HexView()
        self.hexv.setMinimumWidth(self.hexv.cw * 74 + 24)
        self.hexv.setToolTip('Scroll freely - refreshing does not move the view.\\n'
                             'Orange = changed a moment ago, yellow box = PC, blue box = stack pointer')
        ml.addWidget(self.hexv)
        lay.addWidget(self._group('Memory', memw), 3)

        self.vdp = QLabel()
        self.vdp.setFont(mono)
        self.vdp.setTextFormat(Qt.TextFormat.RichText)
        self.vdp.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._group('TMS9918', self.vdp))

        bpw = QWidget()
        bl = QVBoxLayout(bpw)
        bl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.bp_edit = QLineEdit()
        self.bp_edit.setPlaceholderText('hex address, e.g. F668')
        self.bp_edit.setFont(mono)
        self.bp_edit.returnPressed.connect(self.add_bp)
        b_add = QPushButton('Add')
        b_add.clicked.connect(self.add_bp)
        b_del = QPushButton('Remove')
        b_del.clicked.connect(self.del_bp)
        row.addWidget(self.bp_edit, 1)
        row.addWidget(b_add)
        row.addWidget(b_del)
        bl.addLayout(row)
        self.bp_list = QListWidget()
        self.bp_list.setFont(mono)
        self.bp_list.setMaximumHeight(80)
        bl.addWidget(self.bp_list)
        lay.addWidget(self._group('Breakpoints', bpw))
        self.machine = m

    @staticmethod
    def _group(title, w):
        g = QGroupBox(title)
        l = QVBoxLayout(g)
        l.setContentsMargins(6, 4, 6, 6)
        l.addWidget(w)
        return g

    # ---- breakpoints
    def add_bp(self):
        try:
            a = int(self.bp_edit.text().strip().lstrip('$'), 16) & 0xFFFF
        except ValueError:
            return
        self.machine.cpu.breakpoints.add(a)
        self.bp_edit.clear()
        self._fill_bps()
        self.refresh()

    def del_bp(self):
        it = self.bp_list.currentItem()
        if it:
            self.machine.cpu.breakpoints.discard(int(it.text().lstrip('$'), 16))
            self._fill_bps()
            self.refresh()

    def toggle_bp(self, addr):
        bps = self.machine.cpu.breakpoints
        bps.symmetric_difference_update({addr & 0xFFFF})
        self._fill_bps()
        self.refresh()

    def _fill_bps(self):
        self.bp_list.clear()
        for a in sorted(self.machine.cpu.breakpoints):
            self.bp_list.addItem('$%04X' % a)

    # ---- memory view navigation
    def _source_changed(self, _=None):
        self.hexv.verticalScrollBar().setValue(0)
        self.refresh()

    def _goto_edit(self):
        try:
            a = int(self.mem_addr.text().strip().lstrip('$') or '0', 16)
        except ValueError:
            return
        self.hexv.goto(a)

    def _jump(self, key):
        c = self.machine.cpu
        if self.mem_src.currentIndex() == 1:
            return
        a = {'PC': c.pc, 'SP': 0x0100 + c.sp, 'ZP': 0x0000, 'BAS': 0x0400, 'IO': 0x8000, 'VEC': 0xFFF0}[key]
        self.mem_addr.setText('%04X' % a)
        self.hexv.goto(a & ~0xF)

    # ---- refresh
    def refresh(self):
        m = self.machine
        c = m.cpu
        p = c.p
        self._refresh_regs(c, p)
        mem = m.mem
        peek = lambda a: mem[a & 0xFFFF]
        lines, addrs = [], []
        pc = c.pc
        for k in range(18):
            txt, n = disassemble(peek, pc)
            raw, ins = txt[:9].rstrip(), txt[10:].rstrip()
            mn, _, oper = ins.partition(' ')
            if pc in c.breakpoints:
                mark = _span(C_BP, '●', True)
            elif k == 0:
                mark = _span(C_PC, '►', True)
            else:
                mark = '&nbsp;'
            mnc = C_PC if k == 0 else ('#f0a050' if mn in MN_BRANCH else ('#8ad07a' if mn in MN_MEM else C_TXT))
            lines.append('%s %s&nbsp;&nbsp;%s&nbsp;%s&nbsp;%s' % (
                mark, _span(C_ADDR, '%04X' % pc), _span(C_DIM, raw.ljust(8)).replace(' ', '&nbsp;'),
                _span(mnc, mn.ljust(3), k == 0 or mn in MN_BRANCH), _span(C_OPER, oper)))
            addrs.append(pc)
            pc = (pc + n) & 0xFFFF
        html = '<pre style="margin:0">%s</pre>' % '<br>'.join(lines)
        self.dis.set_lines(html, addrs)

        vram = self.mem_src.currentIndex() == 1
        self.hexv.set_source(m.vdp.vram if vram else mem, 0x4000 if vram else 0x10000, not vram)
        self.hexv.pc = c.pc
        self.hexv.sp = 0x0100 + c.sp
        self.hexv.update_data()

        self._refresh_vdp(m)
        self.btn_run.setText('Run' if self.main.paused else 'Pause')

    def _refresh_regs(self, c, p):
        cur = dict(pc=c.pc, a=c.a, x=c.x, y=c.y, sp=c.sp, p=p)
        prev = self._prev_regs or cur

        def val(name, fmt, label):
            ch = cur[name] != prev[name]
            return _span(C_DIM, label) + _span(C_CHG if ch else C_HEX, fmt % cur[name], ch)
        flags = ''.join(_span(C_PC if p & bit else C_DIM, ch, bool(p & bit)) for ch, bit in
                        zip('NV-BDIZC', (0x80, 0x40, 0x20, 0x10, 8, 4, 2, 1)))
        halted = {0: '', 1: '  [WAI]', 2: '  [STP]'}[c.halted]
        nb = '&nbsp;&nbsp;'
        self.regs.setText('%s<br>%s%s%s<br>%s  %s%s' % (
            nb.join((val('pc', '%04X', 'PC '), val('a', '%02X', 'A '), val('x', '%02X', 'X '), val('y', '%02X', 'Y '))),
            val('p', '%02X', 'P '), nb, flags,
            _span(C_DIM, 'SP ') + _span(C_CHG if cur['sp'] != prev['sp'] else C_HEX, '01%02X' % c.sp),
            _span(C_DIM, 'cycles ') + _span(C_TXT, str(c.cycles)), _span(C_BP, halted, True)))
        self._prev_regs = cur

    def _refresh_vdp(self, m):
        v = m.vdp
        r = list(v.regs)
        prev = self._prev_vdp or r
        regs = ' '.join(_span(C_CHG if r[i] != prev[i] else C_HEX, '%02X' % r[i], r[i] != prev[i]) for i in range(len(r)))
        on = v.display_enabled()
        nb = '&nbsp;&nbsp;&nbsp;'
        irq = bool(m.cpu.irq)
        l1 = _span(C_DIM, 'R0-R7 ') + regs
        l2 = nb.join((_span(C_DIM, 'mode ') + _span(C_TXT, MODE_NAMES[v.mode]),
                      _span(C_DIM, 'display ') + _span('#7ad08a' if on else C_BP, 'on' if on else 'blanked')))
        l3 = nb.join((_span(C_DIM, 'status ') + _span(C_HEX, '%02X' % v.status),
                      _span(C_DIM, 'addr ') + _span(C_HEX, '%04X' % v.addr),
                      _span(C_DIM, 'latch ') + _span(C_HEX, str(v.latch))))
        l4 = nb.join((_span(C_DIM, 'IRQ enable ') + _span(C_HEX, str((r[1] >> 5) & 1)),
                      _span(C_DIM, 'IRQ pin ') + _span(C_BP if irq else C_TXT, 'LOW (asserted)' if irq else 'high', irq)))
        self.vdp.setText('<br>'.join((l1, l2, l3, l4)))
        self._prev_vdp = r



# ================================================================== dialogs
class LoadBinDialog(QDialog):
    def __init__(self, parent, path, size):
        super().__init__(parent)
        self.setWindowTitle('Load file into memory')
        f = QFormLayout(self)
        f.addRow(QLabel('%s  (%d bytes)' % (os.path.basename(path), size)))
        self.addr = QLineEdit('0800')
        self.addr.setFont(mono_font())
        f.addRow('Load address $', self.addr)
        self.run = QCheckBox('Set PC to load address (run)')
        f.addRow(self.run)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def address(self):
        return int(self.addr.text().strip().lstrip('$'), 16) & 0xFFFF


HELP_HTML = """
<h3>P65 keyboard</h3>
<p>Keys reach register <b>$8001</b> exactly like in the firmware:
bit 7 = new key, a write to $8001 clears it, up to 7 queued characters,
no auto-repeat. When all keys are released, $8001 reads 0.</p>
<table cellspacing=4>
<tr><td><b>letters without Shift</b></td><td>UPPERCASE (like on the P65)</td></tr>
<tr><td><b>Shift + letter</b></td><td>lowercase</td></tr>
<tr><td><b>Left / Right</b></td><td>codes 17 / 18</td></tr>
<tr><td><b>Esc, Backspace, Tab, Enter</b></td><td>27, 8, 9, 13</td></tr>
<tr><td><b>Ctrl + key</b></td><td>code &amp; $1F (Ctrl+C = break a running program)</td></tr>
<tr><td><b>Ctrl + =</b></td><td>load the BASIC demo program (mapper_init_basic)</td></tr>
</table>
<p>The case inversion can be turned off in the <i>Machine</i> menu.</p>
<h3>Emulator shortcuts</h3>
<table cellspacing=4>
<tr><td><b>F5</b></td><td>CPU reset (RAM is kept)</td></tr>
<tr><td><b>Shift+F5</b></td><td>power cycle (cold start)</td></tr>
<tr><td><b>F6</b> / <b>F7</b></td><td>pause / step one instruction</td></tr>
<tr><td><b>F8</b></td><td>turbo (full speed)</td></tr>
<tr><td><b>F9</b></td><td>load BASIC demo</td></tr>
<tr><td><b>F11</b></td><td>full screen</td></tr>
<tr><td><b>F12</b></td><td>screenshot (PNG)</td></tr>
<tr><td><b>Ctrl+Shift+V</b></td><td>paste clipboard text as typing</td></tr>
<tr><td><b>Ctrl+Shift+O / B</b></td><td>load ROM / type a BASIC program from a file</td></tr>
<tr><td><b>Ctrl+Shift+S / L</b></td><td>save / load state</td></tr>
<tr><td><b>Ctrl+Shift+D</b></td><td>debugger</td></tr>
<tr><td><b>Ctrl+Shift+T</b></td><td>storage manager</td></tr>
<tr><td><b>Ctrl+Shift+G</b></td><td>VDP viewer (tiles, sprites, VRAM map)</td></tr>
</table>
<p>Plain Ctrl+letter goes to the P65, so emulator shortcuts use
Ctrl+Shift and the F keys. Files can also be dropped on the window
(.bin/.rom/.h = ROM, .bas/.txt = typed in, .p65state = state).</p>
<h3>Memory and I/O</h3>
<p>$0000-$7FFF RAM, $8000 LEDs (bits 0-2), $8001 keyboard,
$8002 VDP data, $8003 VDP status/registers, $8100-$FFFF ROM.
Example: <code>POKE 32768,7</code> turns on all LEDs.</p>
<h3>Storage (flash files)</h3>
<p>The ROM can save and load files in the flash storage (an image file in the
emulator, 1 MB at the end of the flash on the device). File names are 8.3.</p>
<table cellspacing=4>
<tr><td><b>SAVE "NAME"</b> / <b>LOAD "NAME"</b></td><td>BASIC program (.BAS)</td></tr>
<tr><td><b>BSAVE "NAME",addr,len[,exec]</b></td><td>memory to a .BIN file</td></tr>
<tr><td><b>BLOAD "NAME"[,addr]</b></td><td>.BIN file to memory (its own address unless given)</td></tr>
<tr><td><b>BRUN "NAME"</b></td><td>load a .BIN and call it (RTS comes back)</td></tr>
<tr><td><b>CATALOG</b>, <b>ERASE "NAME"</b>, <b>RENAME "OLD","NEW"</b></td><td>directory, delete, rename</td></tr>
</table>
<p><b>Ctrl+Shift+T</b> opens the storage manager: the emulator's storage on the
left, the real device (USB) on the right; copy either way, import / export files
of the PC (BASIC text is tokenized on import and listed on export).</p>
<h3>VSYNC interrupt</h3>
<p>The VDP sets the frame flag (bit 7 of $8003) at exactly 60 Hz. With
<b>bit 5 of VDP register 1</b> set, that also pulls the CPU's /IRQ pin low,
until the status register is read. On the real board the pin is driven
through the I2C expander, so it is released a short while <i>after</i> the
read: a handler can be entered again once or twice with bit 7 clear - always
test bit 7 of the status first. The delay is selectable in the <i>Machine</i>
menu. The monitor's IRQ vector jumps through <code>($03FE)</code>.</p>
"""


# ================================================================== main window
class MainWindow(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.settings = QSettings('Portable65', 'P65Emulator')
        self.machine = P65()
        self.settings_irq_us()
        self.storage_dialog = None
        self.machine.open_storage(str(self.settings.value('storage_image', storage_mod.DEFAULT_IMAGE)))
        self.paused = False
        self.turbo = False
        self.swap_case = self.settings.value('swap_case', True, type=bool)
        self.palette_key = self.settings.value('palette', 'lcd')
        if self.palette_key not in PALETTES:
            self.palette_key = 'lcd'
        self.border_mode = self.settings.value('border', 'black')
        self.last_dir = self.settings.value('last_dir', os.path.dirname(os.path.abspath(__file__)))

        self.screen_w = ScreenWidget(self)
        self.screen_w.scale = int(self.settings.value('scale', 3))
        self.screen_w.grid = self.settings.value('grid', False, type=bool)
        self.leds = LedBar()
        self.device = DeviceWidget(self.screen_w, self.leds)
        self.setCentralWidget(self.device)
        self.setAcceptDrops(True)

        self.debug = DebugPanel(self)
        self.dock = QDockWidget('Debugger', self)
        self.dock.setWidget(self.debug)
        self.dock.setObjectName('debugger')
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.hide()
        self.dock.visibilityChanged.connect(self._dock_toggled)

        self.vdpview = VdpViewer(self.machine.vdp, lambda: PALETTES[self.palette_key][1])
        self.vdock = QDockWidget('VDP Viewer', self)
        self.vdock.setObjectName('vdpviewer')
        vscroll = QScrollArea()                                # keeps the dock's preferred size modest; scrolls if smaller
        vscroll.setWidgetResizable(True)
        vscroll.setFrameShape(QFrame.Shape.NoFrame)
        vscroll.setWidget(self.vdpview)
        self.vdock.setWidget(vscroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.vdock)
        self.tabifyDockWidget(self.dock, self.vdock)         # docked next to the debugger (tab); can be dragged out / floated
        self.vdock.hide()
        self.vdock.visibilityChanged.connect(self._vdock_toggled)

        self._build_menus()
        self._build_status()

        # load ROM / boot
        rom_path = args.rom
        if rom_path:
            self._load_rom_file(rom_path, boot=False)
        else:
            data, path = default_rom()
            if data is None:
                QMessageBox.warning(self, APP_NAME, 'ROM not found (include/rom.h).')
            else:
                self.machine.load_rom(data, os.path.relpath(path, os.path.dirname(os.path.dirname(path))))
        if args.storage:
            self.machine.open_storage(args.storage)
            self.settings.setValue('storage_image', args.storage)
        self.machine.power_on()
        if args.state:
            self._load_state_file(args.state)
        if args.type:
            self._type_file(args.type)

        self.setWindowTitle(APP_NAME)
        self._update_rom_label()
        self._refresh_screen(force=True)

        # timing
        self.cycle_debt = 0.0
        self.last_tick = time.perf_counter()
        self.stat_t0 = self.last_tick
        self.stat_cycles = 0
        self.stat_frames = 0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(5)
        self.dbg_timer = QTimer(self)
        self.dbg_timer.timeout.connect(self._debug_tick)
        self.dbg_timer.start(200)

        self.adjustSize()
        self.screen_w.setFocus()

    def settings_irq_us(self):
        try:
            self.machine.irq_release_us = int(self.settings.value('irq_us', self.machine.irq_release_us))
        except (TypeError, ValueError):
            pass

    # ---------------------------------------------------------- UI building
    def _act(self, menu, text, slot, shortcut=None, checkable=False, checked=False, icon=None):
        a = QAction(text, self)
        if icon:
            a.setIcon(icons.icon(icon))
            a.setIconVisibleInMenu(True)
            self.acts[icon] = a
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        if checkable:
            a.setChecked(checked)
            a.toggled.connect(slot)
        else:
            a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _build_menus(self):
        self.acts = {}
        mb = self.menuBar()
        f = mb.addMenu('&File')
        self._act(f, 'Load ROM...', self.open_rom, 'Ctrl+Shift+O', icon='rom')
        self._act(f, 'Restore firmware ROM (include/rom.h)', self.restore_rom)
        self._act(f, 'Load binary file into memory...', self.open_binary)
        f.addSeparator()
        self._act(f, 'Type BASIC program from file...', self.open_basic, 'Ctrl+Shift+B', icon='basic')
        self._act(f, 'Paste clipboard text (as typing)', self.paste_text, 'Ctrl+Shift+V', icon='paste')
        f.addSeparator()
        self._act(f, 'Save state...', self.save_state, 'Ctrl+Shift+S', icon='save_state')
        self._act(f, 'Load state...', self.load_state, 'Ctrl+Shift+L', icon='load_state')
        self._act(f, 'Screenshot (PNG)...', self.screenshot, 'F12', icon='screenshot')
        f.addSeparator()
        self._act(f, 'Quit', self.close, icon='quit')

        m = mb.addMenu('&Machine')
        self._act(m, 'Reset CPU', self.reset_cpu, 'F5', icon='reset')
        self._act(m, 'Power cycle (cold start)', self.power_cycle, 'Shift+F5', icon='power')
        m.addSeparator()
        self.act_pause = self._act(m, 'Pause', self.set_paused, 'F6', checkable=True, icon='pause')
        self._act(m, 'Step (1 instruction)', self.step_instruction, 'F7', icon='step')
        self.act_turbo = self._act(m, 'Turbo (full speed)', self.set_turbo, 'F8', checkable=True, icon='turbo')
        m.addSeparator()
        self._act(m, 'Load BASIC demo (Ctrl+=)', self.init_basic, 'F9', icon='demo')
        self._act(m, 'Unshifted letters are UPPERCASE (like the P65)', self.set_swap_case,
                  checkable=True, checked=self.swap_case)
        m.addSeparator()
        self._act(m, 'Storage manager (emulator / USB device)...', self.show_storage, 'Ctrl+Shift+T', icon='storage')
        self._act(m, 'Choose storage image file...', self.choose_storage_image, icon='storage_file')
        m.addSeparator()
        sub = m.addMenu(icons.icon('delay'), 'VDP interrupt: IRQ release delay')
        ig = QActionGroup(self)
        for us, text in ((0, 'Ideal (no delay)'),
                         (45, 'Fast (~4 empty entries per frame)'),
                         (100, 'Like the board (~7-8 empty entries per frame)'),
                         (150, 'Slow (~10 empty entries per frame)')):
            a = self._act(sub, text, lambda on, us=us: on and self.set_irq_delay(us),
                          checkable=True, checked=self.machine.irq_release_us == us)
            ig.addAction(a)

        v = mb.addMenu('&View')
        sg = QActionGroup(self)
        for s in (1, 2, 3, 4):
            a = self._act(v, 'Scale %dx' % s, lambda on, s=s: on and self.set_scale(s),
                          checkable=True, checked=self.screen_w.scale == s)
            sg.addAction(a)
        self._act(v, 'Full screen', self.toggle_fullscreen, 'F11', icon='fullscreen')
        v.addSeparator()
        pg = QActionGroup(self)
        for key, (name, _) in PALETTES.items():
            a = self._act(v, 'Palette: ' + name, lambda on, k=key: on and self.set_palette(k),
                          checkable=True, checked=self.palette_key == key)
            pg.addAction(a)
        bg = QActionGroup(self)
        for key, name in (('black', 'Black border (like the LCD)'),
                          ('backdrop', 'Border in VDP backdrop colour')):
            a = self._act(v, name, lambda on, k=key: on and self.set_border(k),
                          checkable=True, checked=self.border_mode == key)
            bg.addAction(a)
        self._act(v, 'LCD pixel grid (scale 3x+)', self.set_grid,
                  checkable=True, checked=self.screen_w.grid)
        v.addSeparator()
        a = self.dock.toggleViewAction()
        a.setText('Debugger')
        a.setIcon(icons.icon('debug'))
        self.acts['debug'] = a
        a.setShortcut(QKeySequence('Ctrl+Shift+D'))
        v.addAction(a)
        a = self.vdock.toggleViewAction()
        a.setText('VDP Viewer (tiles, sprites, VRAM)')
        a.setIcon(icons.icon('vdp'))
        self.acts['vdp'] = a
        a.setShortcut(QKeySequence('Ctrl+Shift+G'))
        v.addAction(a)

        h = mb.addMenu('&Help')
        self._act(h, 'Keyboard and shortcuts', self.show_help, 'F1', icon='help')
        self._act(h, 'About', self.show_about, icon='info')
        self._build_toolbar()

    def _build_toolbar(self):
        """One bar of icon buttons (icon + short caption) that repeat the most used menu items."""
        short = {'rom': 'ROM', 'basic': 'BASIC', 'paste': 'Paste', 'save_state': 'Save', 'load_state': 'Load',
                 'screenshot': 'Shot', 'reset': 'Reset', 'power': 'Power', 'pause': 'Pause', 'step': 'Step',
                 'turbo': 'Turbo', 'demo': 'Demo', 'storage': 'Storage', 'fullscreen': 'Full', 'debug': 'Debug',
                 'vdp': 'VDP', 'help': 'Help'}
        layout = ['rom', 'basic', 'paste', None, 'save_state', 'load_state', 'screenshot', None,
                  'reset', 'power', 'pause', 'step', 'turbo', None, 'demo', 'storage', None,
                  'fullscreen', 'debug', 'vdp', None, 'help']
        tb = self.addToolBar('Main')
        tb.setObjectName('maintoolbar')
        tb.setMovable(False)
        tb.setIconSize(QSize(28, 28))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        tb.setStyleSheet('QToolBar { spacing: 2px; padding: 2px; } QToolButton { padding: 3px 6px; font-size: 10px; } '
                         'QToolButton:checked { background: #4a4420; border: 1px solid #ffd400; border-radius: 4px; }')
        for key in layout:
            if key is None:
                tb.addSeparator()
                continue
            a = self.acts[key]
            a.setIconText(short[key])                    # the toolbar uses iconText, the menus keep their long text
            label = a.text().replace('&', '')
            a.setToolTip('%s  (%s)' % (label, a.shortcut().toString()) if not a.shortcut().isEmpty() else label)
            tb.addAction(a)

    def _build_status(self):
        sb = self.statusBar()
        self.lbl_speed = QLabel()
        self.lbl_mode = QLabel()
        self.lbl_kbd = QLabel()
        self.lbl_rom = QLabel()
        for w in (self.lbl_speed, self.lbl_mode, self.lbl_kbd):
            sb.addWidget(w)
        sb.addPermanentWidget(self.lbl_rom)

    def _update_rom_label(self):
        self.lbl_rom.setText('ROM: ' + self.machine.rom_name)

    # ---------------------------------------------------------- emulation loop
    def tick(self):
        now = time.perf_counter()
        dt = min(now - self.last_tick, 0.1)
        self.last_tick = now
        if self.paused:
            self._stats(now)
            return
        m = self.machine
        deadline = now + 0.030           # never block the UI for long
        if self.turbo:
            budget = 1 << 40
            self.cycle_debt = 0
        else:
            self.cycle_debt += dt * CPU_CLOCK
            budget = int(self.cycle_debt)
        done = 0
        rendered = 0
        stopped = False
        while done < budget:
            n, r, stopped = m.run(min(FRAME_CYCLES, budget - done))
            done += n
            rendered += r
            if stopped or time.perf_counter() > deadline:
                break
        if not self.turbo:
            self.cycle_debt -= done
            if self.cycle_debt > CPU_CLOCK * 0.05:      # can't keep up: drop it
                self.cycle_debt = 0
        self.stat_cycles += done
        if rendered:
            self._refresh_screen()
        self.leds.set_state(m.leds)
        if stopped:
            self.set_paused(True)
            self.dock.show()
            self.statusBar().showMessage('Breakpoint at $%04X' % m.cpu.pc, 5000)
        self._stats(now)

    def _stats(self, now):
        if now - self.stat_t0 < 0.5:
            return
        dt = now - self.stat_t0
        mhz = self.stat_cycles / dt / 1e6
        m = self.machine
        # emulated VDP frames (60 Hz) - not just the ones that had to be redrawn
        fps = (m.frames - self.stat_frames) / dt
        self.stat_frames = m.frames
        if self.paused:
            sp = '⏸ paused'
        elif m.cpu.halted:
            sp = '■ CPU halted (%s)' % ('WAI' if m.cpu.halted == 1 else 'STP')
        else:
            sp = '%s %.2f MHz (%d%%)' % ('⏩' if self.turbo else '▶', mhz,
                                          round(mhz * 1e6 / CPU_CLOCK * 100))
        self.lbl_speed.setText(sp + '   ·   %d fps' % round(fps))
        v = m.vdp
        self.lbl_mode.setText('   ·   ' + (MODE_NAMES[v.mode] if v.display_enabled() else 'display blanked'))
        q = m.kbd.pending()
        self.lbl_kbd.setText(('   ·   queued keys: %d' % q) if q else '')
        self.stat_t0 = now
        self.stat_cycles = 0

    def _refresh_screen(self, force=False):
        m = self.machine
        if force:
            m.vdp.render_now()
        pal = PALETTES[self.palette_key][1]
        if self.border_mode == 'backdrop' and m.vdp.display_enabled():
            border = pal[m.vdp.regs[7] & 0x0F]
        else:
            border = 0xFF000000
        self.screen_w.set_frame(m.vdp.frame, pal, border)

    def _dock_toggled(self, visible):
        if visible:
            self.debug.refresh()
        if not self.isFullScreen() and not self.isMaximized():
            # keep the screen at its scale: grow/shrink the window with the dock
            QTimer.singleShot(0, lambda: self.resize(self.sizeHint()))

    def _vdock_toggled(self, visible):
        if visible:
            self.vdpview.refresh()
        if not self.vdock.isFloating() and not self.isFullScreen() and not self.isMaximized():
            QTimer.singleShot(0, lambda: self.resize(self.sizeHint()))

    def _debug_tick(self):
        if self.dock.isVisible() and not self.paused:
            self.debug.refresh()
        if self.vdock.isVisible() and not self.paused:
            self.vdpview.refresh()

    # ---------------------------------------------------------- keyboard
    def handle_key(self, ev, pressed):
        kbd = self.machine.kbd
        kid = ev.nativeScanCode() or ev.key()
        if ev.isAutoRepeat():
            return True                         # the P65 has no key repeat
        if not pressed:
            kbd.release(kid)
            return True
        key = ev.key()
        mods = ev.modifiers()
        ctrl = bool(mods & MOD_CTRL)
        alt = bool(mods & MOD_ALT)
        ch = None
        if key in SPECIAL_KEYS:
            ch = SPECIAL_KEYS[key]
        elif ctrl and not alt:
            if key in (KEY_EQUAL, KEY_PLUS):
                self.init_basic()
                return True
            if KEY_A <= key <= KEY_Z:
                ch = key - KEY_A + 1
            elif 0x20 <= key < 0x7F:
                ch = key & 0x1F
        else:
            t = ev.text()
            if len(t) == 1 and 32 <= ord(t) < 127:
                if self.swap_case and t.isalpha():
                    t = t.swapcase()
                ch = ord(t)
        if ch is None:
            return False
        kbd.press(kid, ch)
        return True

    # ---------------------------------------------------------- actions
    def set_paused(self, on):
        self.paused = bool(on)
        if self.act_pause.isChecked() != self.paused:
            self.act_pause.setChecked(self.paused)
        self.cycle_debt = 0
        self.debug.refresh()
        self.vdpview.refresh()

    def toggle_pause(self):
        self.set_paused(not self.paused)
        self.screen_w.setFocus()

    def step_instruction(self):
        if not self.paused:
            self.set_paused(True)
        self.machine.step()
        self._after_manual_step()

    def step_frame(self):
        if not self.paused:
            self.set_paused(True)
        n, r, stopped = self.machine.run(FRAME_CYCLES)
        self._after_manual_step()

    def _after_manual_step(self):
        self._refresh_screen()
        self.leds.set_state(self.machine.leds)
        self.debug.refresh()
        self.vdpview.refresh()

    def set_turbo(self, on):
        self.turbo = bool(on)
        self.cycle_debt = 0

    def reset_cpu(self):
        self.machine.reset()
        self.statusBar().showMessage('Reset CPU', 2000)
        self.debug.refresh()

    def power_cycle(self):
        self.machine.power_on()
        self._refresh_screen(force=True)
        self.statusBar().showMessage('Cold start', 2000)
        self.debug.refresh()

    def init_basic(self):
        if self.machine.init_basic():
            self.statusBar().showMessage('BASIC demo loaded - type RUN', 4000)
        else:
            self.statusBar().showMessage('No demo program (src/mapper.c)', 4000)

    def show_storage(self):
        if self.machine.store is None:
            QMessageBox.warning(self, APP_NAME, 'Storage is not available: %s' % (self.machine.storage_error or 'unknown error'))
        if self.storage_dialog is None:
            self.storage_dialog = StorageDialog(self, self.machine.store, self.last_dir)
        self.storage_dialog.show()
        self.storage_dialog.raise_()
        self.storage_dialog.activateWindow()

    def choose_storage_image(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Storage image (existing or new)', str(self.settings.value('storage_image', storage_mod.DEFAULT_IMAGE)),
                                              'Storage images (*.img);;All files (*)', options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:
            return
        if self.machine.open_storage(path):
            self.settings.setValue('storage_image', path)
            self.statusBar().showMessage('Storage image: ' + path, 4000)
        else:
            QMessageBox.warning(self, APP_NAME, 'Cannot open storage: %s' % self.machine.storage_error)
        if self.storage_dialog is not None:
            self.storage_dialog.set_store(self.machine.store)
        self.screen_w.setFocus()

    def set_irq_delay(self, us):
        self.machine.irq_release_us = us
        self.settings.setValue('irq_us', us)

    def set_swap_case(self, on):
        self.swap_case = bool(on)
        self.settings.setValue('swap_case', self.swap_case)

    def set_scale(self, s):
        self.screen_w.scale = s
        self.settings.setValue('scale', s)
        if not self.isFullScreen():
            self.showNormal()
            self.screen_w.updateGeometry()
            self.centralWidget().adjustSize()
            self.adjustSize()
            self.resize(self.sizeHint())

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def set_palette(self, key):
        self.palette_key = key
        self.settings.setValue('palette', key)
        self._refresh_screen()

    def set_border(self, key):
        self.border_mode = key
        self.settings.setValue('border', key)
        self._refresh_screen()

    def set_grid(self, on):
        self.screen_w.grid = bool(on)
        self.settings.setValue('grid', self.screen_w.grid)
        self.screen_w.update()

    # ---------------------------------------------------------- files
    def _ask_open(self, title, filt):
        path, _ = QFileDialog.getOpenFileName(self, title, self.last_dir, filt)
        if path:
            self.last_dir = os.path.dirname(path)
            self.settings.setValue('last_dir', self.last_dir)
        self.screen_w.setFocus()
        return path

    def _ask_save(self, title, filt, name):
        path, _ = QFileDialog.getSaveFileName(self, title, os.path.join(self.last_dir, name), filt)
        if path:
            self.last_dir = os.path.dirname(path)
            self.settings.setValue('last_dir', self.last_dir)
        self.screen_w.setFocus()
        return path

    def open_rom(self):
        p = self._ask_open('Load ROM', 'ROM image (*.bin *.rom *.h);;All files (*)')
        if p:
            self._load_rom_file(p)

    def _load_rom_file(self, path, boot=True):
        try:
            if path.lower().endswith('.h'):
                data = load_rom_h(path)
            else:
                with open(path, 'rb') as f:
                    data = f.read()
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, 'Cannot load ROM:\n%s' % e)
            return
        if len(data) > 0x10000 - ROM_START:
            QMessageBox.warning(self, APP_NAME,
                                'The file is %d bytes - the P65 ROM is 32512 ($8100-$FFFF). '
                                'The excess will be truncated.' % len(data))
        self.machine.load_rom(data, os.path.basename(path))
        if boot:
            self.machine.power_on()
            self._refresh_screen(force=True)
            self.statusBar().showMessage('Loaded ROM %s - cold start' % os.path.basename(path), 4000)
        self._update_rom_label()

    def restore_rom(self):
        data, path = default_rom()
        if data:
            self.machine.load_rom(data, os.path.basename(path))
            self.power_cycle()
            self._update_rom_label()

    def open_binary(self):
        p = self._ask_open('Load file into memory', 'Binary files (*.bin *.prg *.obj);;All files (*)')
        if not p:
            return
        with open(p, 'rb') as f:
            data = f.read()
        dlg = LoadBinDialog(self, p, len(data))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            addr = dlg.address()
        except ValueError:
            QMessageBox.warning(self, APP_NAME, 'Invalid address.')
            return
        self.machine.load_binary(data, addr)
        if dlg.run.isChecked():
            self.machine.cpu.pc = addr
            self.machine.cpu.halted = 0
        self.statusBar().showMessage('Loaded %d bytes at $%04X' % (len(data), addr), 4000)
        self.debug.refresh()
        self.screen_w.setFocus()

    def open_basic(self):
        p = self._ask_open('Type BASIC program', 'BASIC program (*.bas *.txt);;All files (*)')
        if p:
            self._type_file(p)

    def _type_file(self, path):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        self._type_text(text)

    def _type_text(self, text):
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        clean = ''.join(ch for ch in text if ch == '\n' or 32 <= ord(ch) < 127)
        clean = clean.replace('\n', '\r')
        self.machine.kbd.type_text(clean)
        self.statusBar().showMessage('Typing %d characters...' % len(clean), 3000)

    def paste_text(self):
        t = QGuiApplication.clipboard().text()
        if t:
            self._type_text(t)

    def save_state(self):
        p = self._ask_save('Save state', 'P65 state (*.p65state)', 'p65.p65state')
        if p:
            try:
                self.machine.save_state(p)
                self.statusBar().showMessage('State saved', 3000)
            except Exception as e:
                QMessageBox.critical(self, APP_NAME, str(e))

    def load_state(self):
        p = self._ask_open('Load state', 'P65 state (*.p65state);;All files (*)')
        if p:
            self._load_state_file(p)

    def _load_state_file(self, p):
        try:
            self.machine.load_state(p)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, 'Cannot load state:\n%s' % e)
            return
        self._update_rom_label()
        self._refresh_screen(force=True)
        self.debug.refresh()

    def screenshot(self):
        p = self._ask_save('Screenshot', 'PNG image (*.png)',
                           time.strftime('p65_%Y%m%d_%H%M%S.png'))
        if p:
            s = self.screen_w.current_scale()
            img = self.screen_w.image.scaled(LCD_W * s, LCD_H * s,
                                             Qt.AspectRatioMode.IgnoreAspectRatio,
                                             Qt.TransformationMode.FastTransformation)
            img.save(p)
            self.statusBar().showMessage('Saved ' + p, 3000)

    # drag & drop
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            p = url.toLocalFile()
            ext = os.path.splitext(p)[1].lower()
            if ext in ('.bas', '.txt'):
                self._type_file(p)
            elif ext == '.p65state':
                self._load_state_file(p)
            elif ext in ('.bin', '.rom', '.h'):
                self._load_rom_file(p)
            break
        self.screen_w.setFocus()

    # ---------------------------------------------------------- help
    def show_help(self):
        d = QDialog(self)
        d.setWindowTitle('Keyboard and shortcuts')
        l = QVBoxLayout(d)
        tb = QTextBrowser()
        tb.setHtml(HELP_HTML)
        l.addWidget(tb)
        d.resize(560, 620)
        d.exec()
        self.screen_w.setFocus()

    def show_about(self):
        QMessageBox.about(self, APP_NAME,
                          '<b>P65 Emulator</b><br>Emulator of the P65 computer:<br>'
                          'WDC 65C02 @ 2 MHz, TMS9918 on a 320x240 LCD,<br>'
                          'keyboard and LEDs through a PCAL6524.<br><br>'
                          'I/O behaviour modelled on the P65 firmware.')

    def closeEvent(self, ev):
        if self.storage_dialog is not None:
            self.storage_dialog.close()
        if self.machine.store is not None:
            self.machine.store.close()
        self.settings.setValue('swap_case', self.swap_case)
        super().closeEvent(ev)


def apply_dark_theme(app):
    app.setStyle('Fusion')
    p = QPalette()
    base = QColor(30, 31, 36)
    p.setColor(QPalette.ColorRole.Window, QColor(24, 25, 29))
    p.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 225))
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(38, 39, 45))
    p.setColor(QPalette.ColorRole.Text, QColor(220, 220, 225))
    p.setColor(QPalette.ColorRole.Button, QColor(44, 45, 52))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 225))
    p.setColor(QPalette.ColorRole.Highlight, QColor(70, 120, 210))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 225))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(120, 120, 130))
    app.setPalette(p)


def main():
    ap = argparse.ArgumentParser(description='P65 emulator')
    ap.add_argument('--rom', help='ROM image (.bin from $8100, or include/rom.h)')
    ap.add_argument('--type', help='text file (e.g. .bas) to type in after start-up')
    ap.add_argument('--state', help='.p65state file to load')
    ap.add_argument('--storage', help='storage image file (default: remembered / emulator/storage.img)')
    args = ap.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_dark_theme(app)
    w = MainWindow(args)
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
