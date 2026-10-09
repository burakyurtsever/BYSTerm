"""assets/icon.png + icon.ico uretir (Qt ile cizilir; bir kez calistirilip commit edilir)."""
import os
import sys
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtGui, QtCore, QtWidgets  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets')


def draw(n):
    img = QtGui.QImage(n, n, QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    r = QtCore.QRectF(n * .04, n * .04, n * .92, n * .92)
    g = QtGui.QLinearGradient(0, 0, 0, n)
    g.setColorAt(0, QtGui.QColor('#2b3140'))
    g.setColorAt(1, QtGui.QColor('#14161c'))
    p.setBrush(g)
    p.setPen(QtGui.QPen(QtGui.QColor('#3fb950'), max(1, n * .03)))
    p.drawRoundedRect(r, n * .18, n * .18)
    f = QtGui.QFont('DejaVu Sans Mono')
    f.setBold(True)
    f.setPixelSize(int(n * .42))
    p.setFont(f)
    p.setPen(QtGui.QColor('#7ee787'))
    p.drawText(QtCore.QRectF(n * .12, n * .10, n * .8, n * .5),
               QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, '>_')
    f.setPixelSize(int(n * .20))
    p.setFont(f)
    p.setPen(QtGui.QColor('#79c0ff'))
    p.drawText(QtCore.QRectF(0, n * .58, n, n * .3), QtCore.Qt.AlignCenter, 'BYS')
    p.end()
    return img


draw(512).save(os.path.join(out, 'icon.png'))
print('ok')
