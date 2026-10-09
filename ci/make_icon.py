#!/usr/bin/env python3
"""BYS aile simgesi — BYSpace'teki tools/bys-simge.ps1'in Python/Qt karsiligi.

Koyu karo, kod yazi tipiyle "BYS_", kenarlarda cip pedleri, pin-1 noktasi.
Tum BYS uygulamalari ayni simgeyi kendi renk ciftiyle kullanir:
    BYSpace    #4FA3FF -> #9B5CFF  (mavi-mor)
    BYS Broker #2BE0A6 -> #F2C94C  (yesil-altin)
    BYSTerm    #FFB547 -> #FF5F6D  (amber-mercan: eski amber terminal ekranlari)

Uretir: assets/icon.png (512), assets/icon.ico (16..256, her boyut ayri cizilir),
        assets/icon.icns (macOS), assets/icon-preview.png (onizleme)
Kullanim: python3 ci/make_icon.py [--renk1 #FFB547 --renk2 #FF5F6D]
Gereksinim: PySide6 (veya PyQt5), Pillow (sadece .icns icin)
"""
import os
import sys
import math
import struct
import argparse

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
OUT = os.path.join(ROOT, 'assets')
FONTS = ['Cascadia Mono', 'Cascadia Code', 'DejaVu Sans Mono', 'Consolas', 'Menlo']


def C(hexs, a=255):
    c = QtGui.QColor(hexs)
    c.setAlpha(a)
    return c


def grad(s, c1, c2, angle=45.0, a=255):
    """System.Drawing LinearGradientBrush(0,0,s,s, aci) esdegeri."""
    r = math.radians(angle)
    dx, dy = math.cos(r), math.sin(r)
    half = (abs(dx) + abs(dy)) * s / 2.0
    c = s / 2.0
    g = QtGui.QLinearGradient(c - dx * half, c - dy * half, c + dx * half, c + dy * half)
    g.setColorAt(0, C(c1, a))
    g.setColorAt(1, C(c2, a))
    return QtGui.QBrush(g)


def rr(x, y, w, h, r):
    p = QtGui.QPainterPath()
    d = max(0.5, min(2 * r, min(w, h)))
    p.addRoundedRect(QtCore.QRectF(x, y, w, h), d / 2, d / 2)
    return p


def text_path(text, family, cx, cy, max_w, max_h):
    f = QtGui.QFont(family)
    f.setBold(True)
    f.setPixelSize(100)
    p = QtGui.QPainterPath()
    p.setFillRule(QtCore.Qt.WindingFill)
    p.addText(0, 0, f, text)
    b = p.boundingRect()
    k = min(max_w / b.width(), max_h / b.height())
    t = QtGui.QTransform()
    t.translate(cx, cy)
    t.scale(k, k)
    t.translate(-(b.x() + b.width() / 2), -(b.y() + b.height() / 2))
    return t.map(p)


def pick_font():
    try:
        fams = set(QtGui.QFontDatabase.families())
    except TypeError:
        fams = set(QtGui.QFontDatabase().families())
    for f in FONTS:
        if f in fams:
            return f
    return 'monospace'


def draw(g, s, c1, c2, font):
    m = max(1.0, s * 0.03)
    g.fillPath(rr(m, m, s - 2 * m, s - 2 * m, s * 0.22), grad(s, '#1A2140', '#070A14', 60))
    acc = grad(s, c1, c2, 35)
    white = QtGui.QBrush(C('#F4F6FF'))

    if s >= 32:   # QFN pedleri: koseler bos, her kenarda 6 ped
        pad = grad(s, c1, c2, 35, 110)
        n, ln, pw, span = 6, s * 0.05, s * 0.034, s * 0.54
        st, edge = (s - span) / 2, s * 0.078
        for i in range(n):
            o = st + span * i / (n - 1) - pw / 2
            r = pw * 0.35
            g.fillPath(rr(o, edge, pw, ln, r), pad)
            g.fillPath(rr(o, s - edge - ln, pw, ln, r), pad)
            g.fillPath(rr(edge, o, ln, pw, r), pad)
            g.fillPath(rr(s - edge - ln, o, ln, pw, r), pad)
    if s >= 48:   # pin-1 isareti
        d = s * 0.05
        e = QtGui.QPainterPath()
        e.addEllipse(QtCore.QRectF(s * 0.17, s * 0.17, d, d))
        g.fillPath(e, acc)
    if s >= 40:
        t = text_path('BYS', font, s * 0.45, s * 0.50, s * 0.54, s * 0.27)
        g.fillPath(t, white)
        bb = t.boundingRect()
        g.fillPath(rr(bb.right() + s * 0.035, bb.bottom() - s * 0.05, s * 0.115, s * 0.05, s * 0.012), acc)
    elif s >= 20:  # orta-kucuk: kalin "B" + imlec, piksele hizali
        t = text_path('B', font, round(s * 0.40), round(s * 0.48), s * 0.40, s * 0.58)
        g.fillPath(t, white)
        bb = t.boundingRect()
        h = max(2, round(s * 0.10))
        g.fillRect(QtCore.QRectF(round(bb.right() + s * 0.07), round(bb.bottom()) - h,
                                 round(s * 0.22), h), acc)
    else:          # 16 px: B harfi piksel piksel, imlec 4x2
        glyph = ['#####.', '##..##', '##..##', '#####.', '##..##', '##..##', '##..##', '#####.']
        k = s / 16.0
        for r, row in enumerate(glyph):
            for c, ch in enumerate(row):
                if ch == '#':
                    g.fillRect(QtCore.QRectF((3 + c) * k, (4 + r) * k, k, k), white)
        g.fillRect(QtCore.QRectF(10 * k, 10 * k, 4 * k, 2 * k), acc)


def render(s, c1, c2, font):
    img = QtGui.QImage(s, s, QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)
    g = QtGui.QPainter(img)
    g.setRenderHint(QtGui.QPainter.Antialiasing)
    draw(g, s, c1, c2, font)
    g.end()
    return img


def png_bytes(img):
    ba = QtCore.QByteArray()
    buf = QtCore.QBuffer(ba)
    buf.open(QtCore.QIODevice.WriteOnly)
    img.save(buf, 'PNG')
    buf.close()
    return bytes(ba)


def write_ico(path, imgs):
    """PNG gomulu ICO (bys-simge.ps1 ile ayni bicim; her boyut ayri cizilmis)."""
    data = [(s, png_bytes(im)) for s, im in imgs]
    out = bytearray(struct.pack('<HHH', 0, 1, len(data)))
    off = 6 + 16 * len(data)
    for s, b in data:
        dim = 0 if s >= 256 else s
        out += struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(b), off)
        off += len(b)
    for _, b in data:
        out += b
    with open(path, 'wb') as f:
        f.write(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--renk1', default='#FFB547')
    ap.add_argument('--renk2', default='#FF5F6D')
    a = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)  # noqa: F841 — font sistemi icin gerekli
    font = pick_font()
    print('font:', font)
    c1, c2 = a.renk1, a.renk2
    os.makedirs(OUT, exist_ok=True)

    render(512, c1, c2, font).save(os.path.join(OUT, 'icon.png'))
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    write_ico(os.path.join(OUT, 'icon.ico'), [(s, render(s, c1, c2, font)) for s in sizes])
    try:
        from PIL import Image
        big = os.path.join(OUT, '_1024.png')
        render(1024, c1, c2, font).save(big)
        Image.open(big).save(os.path.join(OUT, 'icon.icns'))
        os.remove(big)
    except ImportError:
        print('Pillow yok: icon.icns atlandi')

    # onizleme (bys-simge.ps1 -Onizleme ile ayni duzen)
    sheet = QtGui.QImage(760, 300, QtGui.QImage.Format_ARGB32)
    sheet.fill(C('#202020'))
    g = QtGui.QPainter(sheet)
    g.drawImage(16, 22, render(256, c1, c2, font))
    x = 290
    for s in (64, 48, 32, 24, 16):
        g.drawImage(x, 30, render(s, c1, c2, font))
        x += s + 16
    for sz, dx, dy, w in ((32, 290, 130, 128), (16, 440, 130, 128), (24, 590, 154, 96)):
        g.drawImage(QtCore.QRect(dx, dy, w, w), render(sz, c1, c2, font))
    g.end()
    sheet.save(os.path.join(OUT, 'icon-preview.png'))
    print('ok ->', OUT)


if __name__ == '__main__':
    main()
