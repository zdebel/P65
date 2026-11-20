"""Toolbar / menu icons, painted with QPainter (no image files, scale cleanly, fit the dark theme).
Every icon is drawn on a 24x24 grid: light strokes plus one accent colour."""

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

FG = QColor(226, 227, 232)
YELLOW = QColor(255, 212, 0)
GREEN = QColor(88, 214, 124)
RED = QColor(240, 90, 80)
BLUE = QColor(96, 168, 255)
ORANGE = QColor(255, 156, 60)


def _pen(p, color=FG, w=1.8):
    pen = QPen(color, w)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _poly(p, pts, fill=None):
    if fill:
        p.setBrush(fill)
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))


def _folder(p, accent=YELLOW):
    _pen(p, accent)
    p.drawPolyline(QPolygonF([QPointF(3, 19), QPointF(3, 6), QPointF(9, 6), QPointF(11, 8), QPointF(20, 8), QPointF(20, 19), QPointF(3, 19)]))
    _pen(p, FG, 1.4)
    p.drawLine(QPointF(3, 12), QPointF(20, 12))


def _floppy(p, accent):
    _pen(p, accent)
    p.drawPolyline(QPolygonF([QPointF(4, 4), QPointF(17, 4), QPointF(20, 7), QPointF(20, 20), QPointF(4, 20), QPointF(4, 4)]))
    _pen(p, FG, 1.5)
    p.drawRect(QRectF(7, 4, 8, 5))
    p.drawRect(QRectF(7, 13, 10, 7))


def _arrow(p, x, y, up, color):
    _pen(p, color, 2)
    d = -1 if up else 1
    p.drawLine(QPointF(x, y), QPointF(x, y + 7 * d))
    p.drawPolyline(QPolygonF([QPointF(x - 3, y + 4 * d), QPointF(x, y + 7 * d), QPointF(x + 3, y + 4 * d)]))


def draw(name, p):
    if name == 'open':
        _folder(p)
    elif name == 'rom':
        _pen(p, FG)
        p.drawRect(QRectF(5, 3, 14, 18))
        _pen(p, YELLOW, 1.6)
        for y in (7, 11, 15):
            p.drawLine(QPointF(8, y), QPointF(16, y))
    elif name == 'basic':
        _pen(p, FG)
        p.drawRect(QRectF(4, 3, 16, 18))
        _pen(p, GREEN, 1.6)
        p.drawLine(QPointF(7, 8), QPointF(9, 10)); p.drawLine(QPointF(9, 10), QPointF(7, 12))
        p.drawLine(QPointF(11, 13), QPointF(16, 13))
        p.drawLine(QPointF(7, 17), QPointF(14, 17))
    elif name == 'paste':
        _pen(p, FG)
        p.drawRect(QRectF(5, 5, 14, 16))
        _pen(p, YELLOW, 2)
        p.drawRect(QRectF(9, 3, 6, 4))
        _pen(p, FG, 1.5)
        p.drawLine(QPointF(8, 12), QPointF(16, 12)); p.drawLine(QPointF(8, 16), QPointF(14, 16))
    elif name == 'save_state':
        _floppy(p, BLUE)
    elif name == 'load_state':
        _floppy(p, GREEN)
    elif name == 'screenshot':
        _pen(p, FG)
        p.drawRoundedRect(QRectF(3, 7, 18, 13), 2, 2)
        p.drawPolyline(QPolygonF([QPointF(8, 7), QPointF(9.5, 4), QPointF(14.5, 4), QPointF(16, 7)]))
        _pen(p, YELLOW, 1.8)
        p.drawEllipse(QPointF(12, 13.5), 3.6, 3.6)
    elif name == 'reset':
        _pen(p, ORANGE, 2)
        path = QPainterPath()
        path.arcMoveTo(QRectF(4, 4, 16, 16), 60)
        path.arcTo(QRectF(4, 4, 16, 16), 60, 270)
        p.drawPath(path)
        p.setBrush(ORANGE)
        _poly(p, [(16.5, 1.5), (21.5, 6.5), (14.5, 7.5)], ORANGE)
    elif name == 'power':
        _pen(p, RED, 2)
        path = QPainterPath()
        path.arcMoveTo(QRectF(4.5, 5, 15, 15), 120)
        path.arcTo(QRectF(4.5, 5, 15, 15), 120, -300)
        p.drawPath(path)
        p.drawLine(QPointF(12, 3), QPointF(12, 11))
    elif name == 'pause':
        _pen(p, FG, 1)
        p.setBrush(YELLOW)
        p.drawRoundedRect(QRectF(6, 5, 4.5, 14), 1, 1)
        p.drawRoundedRect(QRectF(13.5, 5, 4.5, 14), 1, 1)
    elif name == 'step':
        _pen(p, FG, 1)
        p.setBrush(BLUE)
        _poly(p, [(5, 5), (15, 12), (5, 19)], BLUE)
        p.setBrush(FG)
        p.drawRoundedRect(QRectF(17, 5, 3.5, 14), 1, 1)
    elif name == 'turbo':
        _pen(p, FG, 1)
        _poly(p, [(3, 5), (12, 12), (3, 19)], ORANGE)
        _poly(p, [(12, 5), (21, 12), (12, 19)], ORANGE)
    elif name == 'demo':
        _pen(p, FG)
        p.drawRoundedRect(QRectF(3, 5, 18, 12), 1.5, 1.5)
        p.drawLine(QPointF(8, 20), QPointF(16, 20))
        _poly(p, [(10, 8), (15, 11), (10, 14)], GREEN)
    elif name == 'storage':
        _pen(p, BLUE)
        p.drawEllipse(QRectF(5, 3.5, 14, 5))
        p.drawLine(QPointF(5, 6), QPointF(5, 18)); p.drawLine(QPointF(19, 6), QPointF(19, 18))
        p.drawArc(QRectF(5, 15.5, 14, 5), 0, -180 * 16)
        p.drawArc(QRectF(5, 9.5, 14, 5), 0, -180 * 16)
    elif name == 'storage_file':
        _pen(p, FG)
        p.drawPolyline(QPolygonF([QPointF(6, 3), QPointF(14, 3), QPointF(19, 8), QPointF(19, 21), QPointF(6, 21), QPointF(6, 3)]))
        _pen(p, YELLOW, 1.8)
        p.drawLine(QPointF(14, 3), QPointF(14, 8)); p.drawLine(QPointF(14, 8), QPointF(19, 8))
    elif name == 'fullscreen':
        _pen(p, FG, 2)
        for sx, sy in ((3, 3), (21, 3), (3, 21), (21, 21)):
            dx = 1 if sx < 12 else -1
            dy = 1 if sy < 12 else -1
            p.drawPolyline(QPolygonF([QPointF(sx + 6 * dx, sy), QPointF(sx, sy), QPointF(sx, sy + 6 * dy)]))
    elif name == 'scale':
        _pen(p, FG)
        p.drawRect(QRectF(3, 8, 10, 12))
        _pen(p, YELLOW, 1.8)
        p.drawRect(QRectF(8, 3, 13, 14))
    elif name == 'palette':
        _pen(p, FG, 1)
        for i, c in enumerate((RED, YELLOW, GREEN, BLUE)):
            p.setBrush(c)
            p.drawRect(QRectF(3 + (i % 2) * 9, 3 + (i // 2) * 9, 8, 8))
    elif name == 'debug':
        _pen(p, FG)
        p.drawRect(QRectF(6, 6, 12, 12))
        _pen(p, GREEN, 1.8)
        for t in (9, 12, 15):
            p.drawLine(QPointF(t, 3), QPointF(t, 6)); p.drawLine(QPointF(t, 18), QPointF(t, 21))
            p.drawLine(QPointF(3, t), QPointF(6, t)); p.drawLine(QPointF(18, t), QPointF(21, t))
    elif name == 'vdp':
        _pen(p, FG)
        p.drawRect(QRectF(3, 3, 18, 18))
        _pen(p, YELLOW, 1.4)
        p.drawLine(QPointF(9, 3), QPointF(9, 21)); p.drawLine(QPointF(15, 3), QPointF(15, 21))
        p.drawLine(QPointF(3, 9), QPointF(21, 9)); p.drawLine(QPointF(3, 15), QPointF(21, 15))
    elif name == 'help':
        _pen(p, BLUE, 2)
        p.drawEllipse(QPointF(12, 12), 9, 9)
        _pen(p, FG, 2)
        path = QPainterPath()
        path.moveTo(9, 9.5)
        path.cubicTo(9, 5.5, 15, 5.5, 15, 9.5)
        path.cubicTo(15, 12, 12, 12, 12, 15)
        p.drawPath(path)
        p.drawPoint(QPointF(12, 18))
    elif name == 'info':
        _pen(p, BLUE, 2)
        p.drawEllipse(QPointF(12, 12), 9, 9)
        _pen(p, FG, 2.2)
        p.drawLine(QPointF(12, 11), QPointF(12, 17))
        p.drawPoint(QPointF(12, 7.5))
    elif name == 'quit':
        _pen(p, FG)
        p.drawPolyline(QPolygonF([QPointF(10, 4), QPointF(4, 4), QPointF(4, 20), QPointF(10, 20)]))
        _pen(p, RED, 2)
        p.drawLine(QPointF(9, 12), QPointF(20, 12))
        p.drawPolyline(QPolygonF([QPointF(16, 8), QPointF(20, 12), QPointF(16, 16)]))
    elif name == 'delay':
        _pen(p, FG)
        p.drawEllipse(QPointF(12, 12), 9, 9)
        _pen(p, YELLOW, 2)
        p.drawLine(QPointF(12, 12), QPointF(12, 6)); p.drawLine(QPointF(12, 12), QPointF(16, 14))
    else:
        _pen(p, FG)
        p.drawEllipse(QPointF(12, 12), 8, 8)


_cache = {}


def icon(name, size=48):
    """QIcon for `name` (cached). Falls back to a plain circle for unknown names."""
    key = (name, size)
    if key not in _cache:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(size / 24.0, size / 24.0)
        draw(name, p)
        p.end()
        _cache[key] = QIcon(pm)
    return _cache[key]
