#!/usr/bin/env python3
"""
BYSTerm — Seri port / TCP / UDP hizli test ve izleme araci
(Hercules + Eltima Serial Port Monitor karisimi; Windows / macOS / Linux / Jetson).

  * Seri port   : aktif portlari isim/aciklamalariyla listeler (otomatik yeniler),
                  standart baud'lar, 5-8 bit, parity, stop, akis kontrolu, DTR/RTS, BREAK,
                  CTS/DSR/DCD/RI gostergeleri.
  * TCP istemci : host:port'a baglan.
  * TCP sunucu  : dinle, bagli istemcileri listele, hepsine / secilene gonder, at.
  * UDP         : yerel porttan dinle, hedefe gonder, gelen paketin kaynagini goster/yanitla.
  * Seri izleme : BASKA bir uygulamanin seri trafigini izle (Eltima benzeri) —
                  Linux/macOS'ta sanal port (pty) otomatik olusur; Windows'ta com0com gibi
                  sanal null-modem cifti ile. Iki port pasif dinleme (donanim tap) de var.

Her sekmede: ASCII / HEX / HEX+ASCII gorunum, zaman damgasi, kayit (.log metin / .bin ham),
ASCII (\\r \\n \\xHH kacisli) veya HEX gonderme, satir sonu secimi, periyodik tekrar, dosya gonder.

Yuksek veri akisinda DONMAZ: G/C ayri thread'lerde; ekran tik basina butceli guncellenir,
fazlasi ekranda atlanir ama sayaclar ve kayit dosyasi HER bayti gorur.

Hazir uygulama: GitHub Releases (Windows .exe / macOS .app / Linux / Jetson) — kurulum gerekmez.
Kaynaktan  : pip install pyserial PySide6 ; python3 src/bysterm.py
"""
import os
import sys
import json
import time
import socket
import threading
import subprocess
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if '--net-helper' in sys.argv:          # yonetici yardimcisi (root): Qt/pyserial YUKLENMEZ
    import bysterm_net
    bysterm_net.helper_main(sys.argv[sys.argv.index('--net-helper') + 1:])
    sys.exit(0)

try:
    import serial
    PYSERIAL_VERSION = serial.VERSION
except ImportError:
    sys.stderr.write('pyserial eksik:  pip install pyserial\n')
    sys.exit(1)

QT_API = None
_qt_errors = []
_order = ['PySide6', 'PyQt5', 'PySide2']
if os.environ.get('BYSTERM_QT') in _order:          # zorla: BYSTERM_QT=PyQt5
    _order.insert(0, _order.pop(_order.index(os.environ['BYSTERM_QT'])))
for _api in _order:
    try:
        if _api == 'PySide6':
            from PySide6 import QtCore, QtGui, QtWidgets
        elif _api == 'PyQt5':
            from PyQt5 import QtCore, QtGui, QtWidgets
        else:
            from PySide2 import QtCore, QtGui, QtWidgets
        QT_API = _api
        break
    except ImportError as _e:
        _qt_errors.append(f'  {_api}: {_e}')
        continue
if QT_API is None:
    if getattr(sys, 'frozen', False):
        # paketlenmis uygulama: Qt icinde; eksik olan bir SISTEM kutuphanesidir (grafik)
        sys.stderr.write('Grafik kutuphanesi eksik:\n' + '\n'.join(_qt_errors) + '\n'
                         'Ubuntu/Debian/Jetson:  sudo apt install libgl1 libgles2 libegl1 '
                         'libfontconfig1 libxkbcommon-x11-0\n')
    else:
        sys.stderr.write('Qt yuklenemedi:\n' + '\n'.join(_qt_errors) + '\n'
                         'Kurun:  pip install PySide6\n'
                         '(Jetson / Ubuntu 20.04: sudo apt install python3-pyqt5)\n')
    sys.exit(1)

import bysterm_core as core   # noqa: E402
import bysterm_net as net   # noqa: E402
import bysterm_update as upd   # noqa: E402
import bysterm_usbsniff as usbsniff   # noqa: E402
from bysterm_i18n import tr, tx, tx_exact, set_lang, LANGS   # noqa: E402

from bysterm_core import RX, TX  # noqa: E402

APP_NAME = 'BYSTerm'
APP_VERSION = '0.4.0'

Qt = QtCore.Qt
W = QtWidgets


def qenum(owner, path):
    """Qt5/Qt6 enum uyumu: 'SystemFont.FixedFont' -> once kapsamli, olmazsa duz ad."""
    scope, name = path.split('.')
    sc = getattr(owner, scope, None)
    if sc is not None and hasattr(sc, name):
        return getattr(sc, name)
    return getattr(owner, name)


def qexec(obj):
    return obj.exec() if hasattr(obj, 'exec') else obj.exec_()


# ekranda tik basina gosterilecek en fazla HAM bayt (fazlasi atlanir, sayilir, kaydedilir)
DISPLAY_BUDGET = {'ascii': 24 * 1024, 'hex': 12 * 1024, 'dump': 8 * 1024}
DRAIN_MS = 40
MAX_LINES = 20000

COLORS = {      # KOYU tema terminal renkleri: grafit zemin, kanal renkleri RX=yesil / TX=mavi
    'bg': '#101214', 'fg': '#EDEEEF',
    RX: '#3DDC84', TX: '#4FA8FF', 'hdr': '#5C656D',
    'info': '#F2C94C', 'warn': '#F5B841', 'error': '#F0555B',
}
_COLORS_LIGHT = {    # ACIK tema terminal renkleri (beyaz zeminde okunakli)
    'bg': '#ffffff', 'fg': '#1d2125',
    RX: '#1A7F37', TX: '#0969DA', 'hdr': '#6e7781',
    'info': '#9A6700', 'warn': '#BC4C00', 'error': '#CF222E',
}


def term_colors():
    """Mevcut temaya gore terminal/grafik renkleri."""
    try:
        return COLORS if THEME['dark'] else _COLORS_LIGHT
    except NameError:
        return COLORS


_ips_cache = None
_ips_lock = threading.Lock()


def _ips_fast():
    """Ad cozumlemesi YOK: varsayilan rotanin yerel IP'si (paket gonderilmez) — aninda."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return ips


def _ips_slow():
    # getaddrinfo(hostname) bazi Mac'lerde / bozuk DNS'te ~30 sn bekleyebilir -> sadece arka planda
    global _ips_cache
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    with _ips_lock:
        _ips_cache = _ips_cache | ips


def local_ips():
    """Bu bilgisayarin IPv4 adresleri. Arayuzu ASLA bekletmez."""
    global _ips_cache
    with _ips_lock:
        first = _ips_cache is None
        if first:
            _ips_cache = _ips_fast()
        ips = set(_ips_cache)
    if first:
        threading.Thread(target=_ips_slow, daemon=True, name='local-ips').start()
    ips.discard('0.0.0.0')
    return sorted(ips, key=lambda x: (x.startswith('127.'), x))


# =========================================================================== terminal
class Terminal(W.QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(MAX_LINES)
        self.setLineWrapMode(qenum(W.QPlainTextEdit, 'LineWrapMode.NoWrap'))
        f = QtGui.QFontDatabase.systemFont(qenum(QtGui.QFontDatabase, 'SystemFont.FixedFont'))
        f.setPointSize(max(9, f.pointSize()))
        self.setFont(f)
        self.formats = {}
        self._plain = QtGui.QTextCharFormat()
        self.apply_palette()

    def apply_palette(self):
        c = term_colors()
        border = '#30373E' if (THEME['dark'] if 'THEME' in globals() else True) else '#c5ccd2'
        self.setStyleSheet(f'QPlainTextEdit {{ background:{c["bg"]}; color:{c["fg"]};'
                           f' border:1px solid {border}; }}')
        self.formats = {}
        for k, col in c.items():
            if k in ('bg', 'fg'):
                continue
            tf = QtGui.QTextCharFormat()
            tf.setForeground(QtGui.QColor(col))
            self.formats[k] = tf
        self._plain = QtGui.QTextCharFormat()
        self._plain.setForeground(QtGui.QColor(c['fg']))

    def write_segments(self, segs, autoscroll=True):
        if not segs:
            return
        # ardisik ayni tur segmentleri birlestir -> daha az insertText cagrisi
        merged = []
        for kind, text in segs:
            if merged and merged[-1][0] == kind:
                merged[-1][1].append(text)
            else:
                merged.append((kind, [text]))
        cur = QtGui.QTextCursor(self.document())
        cur.movePosition(qenum(QtGui.QTextCursor, 'MoveOperation.End'))
        cur.beginEditBlock()
        for kind, parts in merged:
            cur.insertText(''.join(parts), self.formats.get(kind, self._plain))
        cur.endEditBlock()
        if autoscroll:
            sb = self.verticalScrollBar()
            sb.setValue(sb.maximum())


# =========================================================================== dil (arayuz metinleri)
_I18N_DONE = False


def install_i18n():
    """Tum arayuz metinlerini secili dile cevir: Qt'nin metin ayarlayan metodlari ve metin alan
    kuruculari sarmalanir (Qt'nin kendi olusturdugu ic widget'lar dahil). Veri (RX/TX baytlari,
    kullanicinin yazdigi degerler) cevrilmez. Dil degisimi yeniden baslatinca gecerli olur."""
    global _I18N_DONE
    if _I18N_DONE:
        return
    _I18N_DONE = True
    core.Formatter.translate = staticmethod(tx)

    def _tx_for(w):
        # Duzenlenebilir kutulardaki ogeler kullanici verisi olabilir (gonderme gecmisi, port adlari):
        # parca degistirme YAPMA, sadece birebir bilinen metinleri cevir. 'raw' isaretliyse hic dokunma.
        if isinstance(w, W.QComboBox):
            if w.property('raw'):
                return None
            if w.isEditable():
                return tx_exact
        return tx

    def first(cls, name):            # ilk str argumani cevir
        orig = getattr(cls, name, None)
        if orig is None:
            return

        def f(self, *a, **k):
            t = _tx_for(self)
            if t is None:
                pass
            elif a and isinstance(a[0], str):
                a = (t(a[0]),) + a[1:]
            elif len(a) >= 2 and not isinstance(a[0], str) and isinstance(a[1], str):
                a = (a[0], t(a[1])) + a[2:]          # addItem(icon, text) / insertItem(i, text)
            return orig(self, *a, **k)
        try:
            setattr(cls, name, f)
        except (TypeError, AttributeError):
            pass

    def listarg(cls, name):          # liste argumani (addItems, basliklar)
        orig = getattr(cls, name, None)
        if orig is None:
            return

        def f(self, items, *a, **k):
            t = _tx_for(self)
            if t is not None:
                items = [t(x) if isinstance(x, str) else x for x in items]
            return orig(self, items, *a, **k)
        try:
            setattr(cls, name, f)
        except (TypeError, AttributeError):
            pass

    for cls, names in ((W.QLabel, ('setText',)), (W.QAbstractButton, ('setText',)),
                       (W.QWidget, ('setToolTip', 'setWindowTitle')),
                       (W.QLineEdit, ('setPlaceholderText',)), (W.QGroupBox, ('setTitle',)),
                       (W.QComboBox, ('addItem', 'insertItem', 'setItemText')),
                       (W.QSpinBox, ('setSuffix', 'setPrefix', 'setSpecialValueText')),
                       (W.QDoubleSpinBox, ('setSuffix', 'setPrefix', 'setSpecialValueText')),
                       (W.QMenu, ('addAction', 'addMenu', 'setTitle')), (W.QToolBar, ('addAction',)),
                       (W.QStatusBar, ('showMessage',)), (W.QMessageBox, ('setText', 'setWindowTitle')),
                       (W.QTableWidgetItem, ('setText', 'setToolTip')), (W.QListWidgetItem, ('setText',))):
        for n in names:
            first(cls, n)
    listarg(W.QComboBox, 'addItems')
    listarg(W.QTableWidget, 'setHorizontalHeaderLabels')
    # statik diyaloglar
    for n in ('question', 'warning', 'information', 'critical'):
        orig = getattr(W.QMessageBox, n)

        def mk(orig):
            def f(parent, title, text, *a, **k):
                return orig(parent, tx(title), tx(text), *a, **k)
            return staticmethod(f)
        setattr(W.QMessageBox, n, mk(orig))
    for n in ('getSaveFileName', 'getOpenFileName'):
        orig = getattr(W.QFileDialog, n)

        def mk(orig):
            def f(parent=None, caption='', directory='', filt='', *a, **k):
                return orig(parent, tx(caption), directory, tx(filt), *a, **k)
            return staticmethod(f)
        setattr(W.QFileDialog, n, mk(orig))
    orig_gt = W.QInputDialog.getText

    def get_text(parent, title, label, *a, **k):
        return orig_gt(parent, tx(title), tx(label), *a, **k)
    W.QInputDialog.getText = staticmethod(get_text)

    # metin alan kurucular
    def ctor(base):
        class T(base):
            def __init__(self, *a, **k):
                if a and isinstance(a[0], str):
                    a = (tx(a[0]),) + a[1:]
                super().__init__(*a, **k)
        T.__name__ = base.__name__
        T.__qualname__ = base.__qualname__
        return T
    for name in ('QLabel', 'QPushButton', 'QCheckBox', 'QRadioButton', 'QGroupBox', 'QTableWidgetItem',
                 'QListWidgetItem'):
        setattr(W, name, ctor(getattr(W, name)))


# =========================================================================== akan yerlesim
class FlowLayout(W.QLayout):
    """Dar pencerede alt satira kayan yatay yerlesim (Terminator tarzi bolmeler icin).

    * Bir QLabel ile ardindan gelen kutu BIRLIKTE tasinir ("Baud [115200]" bolunmez).
    * addWidget(w, stretch>0) olan ogeler satirda kalan boslugu doldurur.
    * QHBoxLayout'un addWidget/addSpacing/addStretch cagrilariyla uyumludur.
    """

    def __init__(self, parent=None, hspacing=6, vspacing=4):
        super().__init__(parent)
        self._items = []
        self._stretch = {}
        self.hs, self.vs = hspacing, vspacing
        self._h = 0          # mevcut genislikte gereken yukseklik (ust yerlesime bildirilir)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def addWidget(self, w, stretch=0, *args, **kw):
        super().addWidget(w)
        if stretch and self._items:
            self._stretch[id(self._items[-1])] = stretch

    def addSpacing(self, n):
        self.addItem(W.QSpacerItem(int(n), 1))

    def addStretch(self, *args):
        pass

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        if 0 <= i < len(self._items):
            it = self._items.pop(i)
            self._stretch.pop(id(it), None)
            return it
        return None

    def expandingDirections(self):
        for f in (lambda: Qt.Orientations(0), lambda: Qt.Orientation(0)):
            try:
                return f()
            except Exception:
                continue
        return 0

    def hasHeightForWidth(self):
        return False      # Qt ic ice heightForWidth'i guvenilir yaymiyor: yukseklik _h ile bildirilir

    def heightForWidth(self, width):
        return self._do(QtCore.QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        h = self._do(rect, False)
        if h != self._h:
            self._h = h
            QtCore.QTimer.singleShot(0, self._notify)

    def _notify(self):
        try:
            self.invalidate()
            w = self.parentWidget()
            while w is not None:
                w.updateGeometry()
                if isinstance(w, Pane):
                    break
                w = w.parentWidget()
        except RuntimeError:      # widget silinmis
            pass

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        w = h = 0
        for unit in self._units():
            uw = sum(it.minimumSize().width() if not it.spacerItem() else 0 for it in unit) + self.hs * (len(unit) - 1)
            w = max(w, uw)
            h = max(h, max(it.minimumSize().height() for it in unit))
        m = self.contentsMargins()
        return QtCore.QSize(w + m.left() + m.right(), max(h + m.top() + m.bottom(), self._h))

    def _units(self):
        vis = [it for it in self._items if it.spacerItem() is not None or not it.isEmpty()]
        units, i = [], 0
        while i < len(vis):
            it = vis[i]
            wdg = it.widget()
            if isinstance(wdg, W.QLabel) and not wdg.wordWrap() and i + 1 < len(vis):
                units.append([it, vis[i + 1]])
                i += 2
            else:
                units.append([it])
                i += 1
        return units

    def _do(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        width = max(1, r.width())
        lines, cur, cur_w = [], [], 0
        for unit in self._units():
            uw = sum(min(it.sizeHint().width(), width) for it in unit) + self.hs * (len(unit) - 1)
            if cur and cur_w + self.hs + uw > width:
                lines.append(cur)
                cur, cur_w = [], 0
            cur_w += (self.hs if cur else 0) + uw
            cur.append(unit)
        if cur:
            lines.append(cur)
        y = r.y()
        for line in lines:
            items = [it for unit in line for it in unit]
            hints = [QtCore.QSize(min(it.sizeHint().width(), width), it.sizeHint().height()) for it in items]
            used = sum(h.width() for h in hints) + self.hs * (len(items) - 1)
            extra = max(0, width - used)
            st = [self._stretch.get(id(it), 0) for it in items]
            tot = sum(st)
            widths = [h.width() + (extra * s // tot if tot else 0) for h, s in zip(hints, st)]
            # ic ice akan yerlesim (ornek: seri ayar paneli) dar alanda kendi icinde satir atlar:
            # yuksekligi verilen genislige gore sor (heightForWidth)
            heights = [it.heightForWidth(w) if it.hasHeightForWidth() else h.height()
                       for it, h, w in zip(items, hints, widths)]
            heights = [h if h > 0 else hh.height() for h, hh in zip(heights, hints)]
            lh = max(heights)
            if not test:
                x = r.x()
                for it, w, h in zip(items, widths, heights):
                    it.setGeometry(QtCore.QRect(x, y + (lh - h) // 2, w, h))
                    x += w + self.hs
            y += lh + self.vs
        return y - self.vs - r.y() + m.top() + m.bottom() if lines else m.top() + m.bottom()


# =========================================================================== seri ayar paneli
class SerialSettings(W.QWidget):
    """Port + baud + format secimi (Seri ve Seri izleme sekmelerinde ortak)."""

    def __init__(self, main, with_port=True, parent=None):
        super().__init__(parent)
        self.main = main
        lay = FlowLayout(self)

        self.port = W.QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(200)
        self.port.setSizeAdjustPolicy(qenum(W.QComboBox, 'SizeAdjustPolicy.AdjustToContents'))
        self.port.lineEdit().setPlaceholderText('Port sec veya yaz (COM3, /dev/ttyUSB0)')
        self.btn_refresh = W.QToolButton()
        self.btn_refresh.setText('⟳')
        self.btn_refresh.setToolTip('Port listesini simdi yenile (liste zaten otomatik yenilenir)')
        self.btn_refresh.clicked.connect(main.rescan_ports_now)

        self.baud = W.QComboBox()
        self.baud.setEditable(True)
        for b in core.STANDARD_BAUDS:
            self.baud.addItem(str(b))
        self.baud.setCurrentText('115200')
        self.baud.setValidator(QtGui.QIntValidator(50, 20000000, self))
        self.bits = W.QComboBox()
        self.bits.addItems(['8', '7', '6', '5'])
        self.parity = W.QComboBox()
        for k in core.PARITIES:              # gorunen metin cevrilir, anahtar itemData'da kalir
            self.parity.addItem(k, k)
        self.stop = W.QComboBox()
        for k in core.STOPBITS:
            self.stop.addItem(k, k)
        self.flow = W.QComboBox()
        for k in core.FLOWS:
            self.flow.addItem(k, k)

        if with_port:
            lay.addWidget(W.QLabel('Port'))
            lay.addWidget(self.port, 1)
            lay.addWidget(self.btn_refresh)
        for lbl, w in (('Baud', self.baud), ('Bit', self.bits), ('Parity', self.parity),
                       ('Stop', self.stop), ('Akis', self.flow)):
            lay.addWidget(W.QLabel(lbl))
            lay.addWidget(w)
        self.update_ports(main.ports)

    def current_device(self):
        i = self.port.currentIndex()
        txt = self.port.currentText().strip()
        if i >= 0 and self.port.itemText(i) == txt:
            return self.port.itemData(i) or txt
        return txt.split('  —  ')[0].split('  [')[0].strip()

    def update_ports(self, ports):
        if self.port.view().isVisible():
            return False                   # acik listeyi kullanici gezerken bozma
        cur = self.current_device()
        typed = self.port.currentText().strip()
        self.port.blockSignals(True)
        self.port.clear()
        sel = -1
        for i, p in enumerate(ports):
            self.port.addItem(p.label, p.device)
            self.port.setItemData(i, p.tooltip, qenum(Qt, 'ItemDataRole.ToolTipRole'))
            if p.device == cur:
                sel = i
        if sel >= 0:
            self.port.setCurrentIndex(sel)
        elif typed and cur:
            self.port.setEditText(cur)      # listede olmayan elle yazilmis yol
        elif ports:
            self.port.setCurrentIndex(0)
        self.port.blockSignals(False)
        return True

    def config(self):
        dev = self.current_device()
        if not dev:
            raise ValueError('Port secilmedi')
        try:
            baud = int(self.baud.currentText())
        except ValueError:
            raise ValueError('Gecersiz baud') from None
        return core.SerialConfig(dev, baud, int(self.bits.currentText()),
                                 core.PARITIES[self.parity.currentData()],
                                 core.STOPBITS[self.stop.currentData()],
                                 self.flow.currentData())

    def save(self, st, prefix):
        st.setValue(f'{prefix}/port', self.current_device())
        st.setValue(f'{prefix}/baud', self.baud.currentText())
        st.setValue(f'{prefix}/bits', self.bits.currentText())
        for k in ('parity', 'stop', 'flow'):
            st.setValue(f'{prefix}/{k}', getattr(self, k).currentData())

    def load(self, st, prefix):
        dev = st.value(f'{prefix}/port', '')
        if dev:
            idx = self.port.findData(dev)
            if idx >= 0:
                self.port.setCurrentIndex(idx)
        v = st.value(f'{prefix}/baud', None)
        if v:
            self.baud.setCurrentText(str(v))
        v = st.value(f'{prefix}/bits', None)
        if v and self.bits.findText(str(v)) >= 0:
            self.bits.setCurrentIndex(self.bits.findText(str(v)))
        for k in ('parity', 'stop', 'flow'):
            v = st.value(f'{prefix}/{k}', None)
            cb = getattr(self, k)
            if v is not None:
                i = cb.findData(str(v))
                if i < 0:
                    i = cb.findText(str(v))       # eski surum ayari (metin olarak kaydedilmis)
                if i >= 0:
                    cb.setCurrentIndex(i)


# =========================================================================== oturum tabani
class Session(W.QWidget):
    KIND = 'base'
    TITLE = 'Oturum'
    HAS_SEND = True

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.transport = None
        self.connected = False
        self.rx_total = self.tx_total = 0
        self._rate_prev = (time.monotonic(), 0, 0)
        self.rx_rate = self.tx_rate = 0.0
        self.skipped = 0
        self._skip_reported = time.monotonic()
        self.log_file = None
        self.log_bin = False
        self.log_fmt = None

        root = W.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 4)
        root.setSpacing(4)

        # --- baglanti kutusu
        self.conn_box = W.QGroupBox('Baglanti')
        cl = W.QVBoxLayout(self.conn_box)
        cl.setContentsMargins(6, 4, 6, 4)
        row = FlowLayout()
        self.build_connection(row, cl)
        self.btn_connect = W.QPushButton(self.connect_text())
        self.btn_connect.setMinimumWidth(110)
        self.btn_connect.setObjectName('primary')
        self.btn_connect.setDefault(False)
        self.btn_connect.clicked.connect(self.toggle_connection)
        row.addWidget(self.btn_connect)
        cl.insertLayout(0, row)
        root.addWidget(self.conn_box)

        # --- gorunum secenekleri
        opt = FlowLayout()
        self.mode = W.QComboBox()
        self.mode.addItem('ASCII', 'ascii')
        self.mode.addItem('HEX', 'hex')
        self.mode.addItem('HEX + ASCII', 'dump')
        self.mode.currentIndexChanged.connect(self._fmt_changed)
        self.chk_ts = W.QCheckBox('Zaman damgasi')
        self.chk_ts.setChecked(True)
        self.chk_ts.toggled.connect(self._fmt_changed)
        self.chk_tx = W.QCheckBox('Gonderileni goster')
        self.chk_tx.setChecked(True)
        self.chk_ctrl = W.QCheckBox('Kontrol karakteri <XX>')
        self.chk_ctrl.setChecked(True)
        self.chk_ctrl.setToolTip('ASCII gorunumde yazdirilamayan baytlari <0D> seklinde goster '
                                 '(kapaliysa ".")')
        self.chk_ctrl.toggled.connect(self._fmt_changed)
        self.chk_scroll = W.QCheckBox('Oto-kaydir')
        self.chk_scroll.setChecked(True)
        self.btn_pause = W.QPushButton('Duraklat')
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip('Ekrani dondur (veri yine sayilir ve kaydedilir)')
        self.btn_clear = W.QPushButton('Temizle')
        self.btn_clear.clicked.connect(self.clear)
        self.btn_save = W.QPushButton('Ekrani kaydet...')
        self.btn_save.clicked.connect(self.save_view)
        self.btn_log = W.QPushButton('● Kayit')
        self.btn_log.setCheckable(True)
        self.btn_log.setToolTip('Gelen/giden her seyi dosyaya yaz.\n'
                                '.log/.txt = metin (zaman damgali, secili gorunumde)\n'
                                '.bin = ham alinan baytlar (RX)')
        self.btn_log.toggled.connect(self.toggle_log)
        opt.addWidget(W.QLabel('Gorunum'))
        for w in (self.mode, self.chk_ts, self.chk_tx, self.chk_ctrl, self.chk_scroll):
            opt.addWidget(w)
        opt.addStretch(1)
        for w in (self.btn_pause, self.btn_clear, self.btn_save, self.btn_log):
            opt.addWidget(w)
        root.addLayout(opt)

        # --- terminal (+ istege bagli yan panel)
        self.term = Terminal()
        side = self.build_side_panel()
        if side is not None:
            split = W.QSplitter(qenum(Qt, 'Orientation.Horizontal'))
            split.addWidget(self.term)
            split.addWidget(side)
            split.setStretchFactor(0, 4)
            split.setStretchFactor(1, 1)
            root.addWidget(split, 1)
        else:
            root.addWidget(self.term, 1)

        # --- gonderme satiri
        self.send_box = W.QWidget()
        sl = FlowLayout(self.send_box)
        self.send_edit = W.QComboBox()
        self.send_edit.setEditable(True)
        self.send_edit.setProperty('raw', True)     # gonderilecek veri: asla cevrilmez
        self.send_edit.setInsertPolicy(qenum(W.QComboBox, 'InsertPolicy.NoInsert'))
        self.send_edit.setMaxCount(50)
        self.send_edit.setMinimumWidth(220)
        self.send_edit.lineEdit().setPlaceholderText('Gonderilecek veri (Enter = gonder)')
        self.send_edit.lineEdit().returnPressed.connect(self.send_now)
        self.send_mode = W.QComboBox()
        self.send_mode.addItems(['ASCII', 'HEX'])
        self.send_mode.setToolTip('HEX ornek: 01 03 00 00 00 0A C5 CD  veya 0103000A')
        self.eol = W.QComboBox()
        self.eol.addItems(['Sonek yok', 'CR', 'LF', 'CR+LF'])
        self.eol.setToolTip('ASCII gonderimde sona eklenecek satir sonu')
        self.chk_esc = W.QCheckBox('\\ kacis')
        self.chk_esc.setChecked(True)
        self.chk_esc.setToolTip('ASCII icinde \\r \\n \\t \\0 \\xHH kullanilabilir')
        self.btn_send = W.QPushButton('Gonder')
        self.btn_send.clicked.connect(self.send_now)
        self.chk_repeat = W.QCheckBox('Tekrar')
        self.chk_repeat.toggled.connect(self._repeat_toggled)
        self.repeat_ms = W.QSpinBox()
        self.repeat_ms.setRange(1, 3600000)
        self.repeat_ms.setValue(1000)
        self.repeat_ms.setSuffix(' ms')
        self.repeat_ms.valueChanged.connect(lambda v: self.repeat_timer.setInterval(v))
        self.btn_file = W.QPushButton('Dosya gonder...')
        self.btn_file.clicked.connect(self.send_file)
        sl.addWidget(self.send_edit, 1)
        for w in (self.send_mode, self.eol, self.chk_esc, self.btn_send, self.chk_repeat,
                  self.repeat_ms, self.btn_file):
            sl.addWidget(w)
        self.build_send_extras(sl)
        root.addWidget(self.send_box)
        self.send_box.setVisible(self.HAS_SEND)
        self.chk_tx.setVisible(self.HAS_SEND)

        # --- durum satiri
        st = W.QHBoxLayout()
        self.lbl_stats = W.QLabel()
        self.lbl_stats.setTextInteractionFlags(qenum(Qt, 'TextInteractionFlag.TextSelectableByMouse'))
        self.btn_reset = W.QToolButton()
        self.btn_reset.setText('Sayac sifirla')
        self.btn_reset.clicked.connect(self.reset_counters)
        self.lbl_state = W.QLabel('Kapali')
        st.addWidget(self.lbl_stats, 1)
        st.addWidget(self.btn_reset)
        st.addWidget(self.lbl_state)
        root.addLayout(st)

        self.fmt = core.Formatter(labels={k: tx(v) for k, v in self.labels().items()})
        self._fmt_changed()

        # --- zamanlayicilar
        self.drain_timer = QtCore.QTimer(self)
        self.drain_timer.timeout.connect(self.drain)
        self.drain_timer.start(DRAIN_MS)
        self.stats_timer = QtCore.QTimer(self)
        self.stats_timer.timeout.connect(self.update_stats)
        self.stats_timer.start(1000)
        self.repeat_timer = QtCore.QTimer(self)
        self.repeat_timer.timeout.connect(lambda: self.send_now(from_timer=True))

        self.load_settings()
        self.update_stats()
        self.set_connected(False)

    # ------------------------------------------------------------- alt sinif kancalari
    def build_connection(self, row, col):
        pass

    def build_side_panel(self):
        return None

    def build_send_extras(self, layout):
        pass

    def make_transport(self):
        raise NotImplementedError

    def set_inputs_enabled(self, enabled):
        pass

    def connect_text(self):
        return 'Ac'

    def disconnect_text(self):
        return 'Kapat'

    def labels(self):
        return {RX: 'RX', TX: 'TX'}

    def send_target(self):
        return None

    def on_state(self, state, text):
        pass

    def tab_label(self):
        return self.TITLE

    def update_ports(self, ports):
        pass

    def save_settings(self, st):
        pass

    def load_settings_into(self, st):
        pass

    # ------------------------------------------------------------- ayarlar
    def _prefix(self):
        return self.KIND

    def load_settings(self):
        st = self.main.settings
        p = self._prefix()
        try:
            v = st.value(f'{p}/mode', None)
            if v is not None:
                i = self.mode.findData(v)
                if i >= 0:
                    self.mode.setCurrentIndex(i)
            for key, w in (('ts', self.chk_ts), ('tx', self.chk_tx), ('esc', self.chk_esc)):
                v = st.value(f'{p}/{key}', None)
                if v is not None:
                    w.setChecked(str(v).lower() in ('1', 'true'))
            v = st.value(f'{p}/send_mode', None)
            if v is not None and self.send_mode.findText(str(v)) >= 0:
                self.send_mode.setCurrentIndex(self.send_mode.findText(str(v)))
            v = st.value(f'{p}/eol_i', None)
            if v is not None and 0 <= int(v) < self.eol.count():
                self.eol.setCurrentIndex(int(v))
            hist = st.value(f'{p}/history', None)
            if hist:
                if isinstance(hist, str):
                    hist = [hist]
                for h in list(hist)[:50]:
                    self.send_edit.addItem(str(h))
                self.send_edit.setEditText('')
            self.load_settings_into(st)
        except Exception:
            pass

    def store_settings(self):
        st = self.main.settings
        p = self._prefix()
        st.setValue(f'{p}/mode', self.mode.currentData())
        st.setValue(f'{p}/ts', self.chk_ts.isChecked())
        st.setValue(f'{p}/tx', self.chk_tx.isChecked())
        st.setValue(f'{p}/esc', self.chk_esc.isChecked())
        st.setValue(f'{p}/send_mode', self.send_mode.currentText())
        st.setValue(f'{p}/eol_i', self.eol.currentIndex())
        st.setValue(f'{p}/history', [self.send_edit.itemText(i) for i in range(self.send_edit.count())])
        self.save_settings(st)

    # ------------------------------------------------------------- baglanti
    def toggle_connection(self):
        if self.transport is not None:
            self.close_conn()
        else:
            self.open_conn()

    def open_conn(self):
        try:
            t = self.make_transport()
        except ValueError as e:
            self.info(str(e), 'error')
            return
        self.store_settings()
        t.labels = self.labels() if not getattr(t, 'labels', None) else t.labels
        t.labels = {k: tx(v) for k, v in t.labels.items()}
        self.fmt.labels = t.labels
        self.transport = t
        self.set_busy()
        t.start()

    def close_conn(self, reason=None):
        t = self.transport
        if t is None:
            return
        self.transport = None
        self.repeat_timer.stop()
        self.chk_repeat.setChecked(False)
        self.set_connected(False)
        self.btn_connect.setEnabled(False)
        self.lbl_state.setText('Kapatiliyor...')

        def done(_r, err):
            # kapatma arka planda: surucu takilsa bile arayuz donmaz
            self._drain_transport(t)       # kapanirken gelen son veriler
            self.info(reason or 'Baglanti kapatildi', 'error' if err else 'info')
            if err:
                self.info(f'Kapatma hatasi: {err}', 'error')
            self.btn_connect.setEnabled(True)
            self.set_connected(self.transport is not None and self.connected)
        _bg(self, t.close, done)

    def set_busy(self):
        self.btn_connect.setText('Iptal')
        self.lbl_state.setText('Aciliyor...')
        self.set_inputs_enabled(False)

    def set_connected(self, on):
        self.connected = on
        self.btn_connect.setText(self.disconnect_text() if on else self.connect_text())
        if on:
            self.lbl_state.setText(f'<b style="color:#3DDC84">● ACIK</b>  {self.transport.description}')
        else:
            self.lbl_state.setText('<span style="color:#888">○ Kapali</span>')
            self.set_inputs_enabled(True)
        for w in (self.btn_send, self.btn_file, self.chk_repeat):
            w.setEnabled(on)
        self.main.session_state_changed(self)

    # ------------------------------------------------------------- veri akisi
    def drain(self):
        t = self.transport
        if t is not None:
            self._drain_transport(t)

    def _drain_transport(self, t):
        evq = t.events
        n = len(evq)
        if not n:
            return
        display = []          # ('data', ts, dir, data, peer) | ('line', ts, kind, text)
        disp_bytes = 0
        show_tx = self.chk_tx.isChecked() or not self.HAS_SEND
        log = self.log_file
        log_parts = [] if (log and not self.log_bin) else None
        closed = None
        for _ in range(n):
            ev = evq.popleft()
            et = ev[0]
            if et == 'data':
                _, ts, d, data, peer = ev
                if d == RX:
                    self.rx_total += len(data)
                else:
                    self.tx_total += len(data)
                if log is not None:
                    if self.log_bin:
                        if d == RX:
                            log.write(data)
                    else:
                        for _k, txt in self.log_fmt.format(ts, d, data, peer):
                            log_parts.append(txt)
                if d == TX and not show_tx:
                    continue
                display.append(ev)
                disp_bytes += len(data)
            elif et == 'info':
                _, ts, level, text = ev
                display.append(('line', ts, level, text))
                if log_parts is not None:
                    log_parts.extend(x[1] for x in self.log_fmt.text_line(ts, level, text))
            elif et == 'state':
                _, ts, state, text = ev
                if state == 'open':
                    if t is self.transport:
                        self.set_connected(True)
                    display.append(('line', ts, 'info', text))
                else:
                    kind = 'info'
                    msg = {'client+': f'Istemci baglandi: {text}',
                           'client-': f'Istemci ayrildi: {text}'}.get(state, text)
                    display.append(('line', ts, kind, msg))
                self.on_state(state, text)
                if log_parts is not None:
                    log_parts.extend(x[1] for x in self.log_fmt.text_line(ts, 'info', display[-1][3]))
            elif et == 'closed':
                closed = ev
                display.append(('line', ev[1], 'error', ev[2]))
        if log_parts:
            try:
                log.write(''.join(log_parts))
            except (OSError, ValueError):
                pass

        if self.btn_pause.isChecked():
            self.skipped += disp_bytes
            display = [e for e in display if e[0] == 'line']
        else:
            budget = DISPLAY_BUDGET.get(self.fmt.mode, 16384)
            if disp_bytes > budget:
                # sadece SON `budget` bayti goster (en yeni veri en onemlisi)
                keep, acc = [], 0
                for e in reversed(display):
                    if e[0] == 'data':
                        if acc >= budget:
                            self.skipped += len(e[3])
                            continue
                        room = budget - acc
                        if len(e[3]) > room:
                            self.skipped += len(e[3]) - room
                            e = (e[0], e[1], e[2], e[3][-room:], e[4])
                        acc += len(e[3])
                    keep.append(e)
                display = keep[::-1]
                self.fmt.last_key = None      # atlama sonrasi yeni satirdan basla
        segs = []
        now = time.monotonic()
        if self.skipped and now - self._skip_reported >= 1.0 and not self.btn_pause.isChecked():
            segs += self.fmt.text_line(time.time(), 'warn',
                                       f'... {core.human_bytes(self.skipped)} ekranda gosterilmedi '
                                       f'(cok yuksek hiz; sayac ve kayit eksiksiz)')
            self.skipped = 0
            self._skip_reported = now
        for e in display:
            if e[0] == 'data':
                segs += self.fmt.format(e[1], e[2], e[3], e[4])
            else:
                segs += self.fmt.text_line(e[1], e[2], e[3])
        self.term.write_segments(segs, self.chk_scroll.isChecked() and not self.btn_pause.isChecked())

        if closed is not None and t is self.transport:
            self.transport = None
            self.repeat_timer.stop()
            self.chk_repeat.setChecked(False)
            t.close()
            self.set_connected(False)
            self._maybe_fix_permission(t, closed[2])

    def _maybe_fix_permission(self, t, reason):
        """Linux/macOS: seri porta erisim reddedildiyse yonetici yardimcisiyla izin ver ve yeniden ac."""
        cfg = getattr(t, 'cfg', None)
        if net.IS_WIN or cfg is None or 'Erisim reddedildi' not in reason or getattr(self, '_perm_try', False):
            return
        dev = cfg.port
        h = net.PrivHelper.get()
        if not h.alive:
            r = W.QMessageBox.question(self, 'Seri port izni',
                                       f'{dev} portuna erisim izniniz yok.\n\nIzin verilsin mi? (yonetici sifresi bir kez sorulur; '
                                       f'kullaniciniz kalici olarak "dialout" grubuna da eklenir)')
            if r != qenum(W.QMessageBox, 'StandardButton.Yes'):
                return
        self._perm_try = True
        self.info(f'{dev}: erisim izni veriliyor...')

        def work():
            if not h.alive and not h.start():
                return False, f'yonetici izni alinamadi ({h.err})'
            return h.grant_serial(dev)

        def done(res, err):
            self.main._admin_changed()
            ok, out = res if res else (False, str(err))
            if ok:
                self.info(f'{dev}: izin verildi, port yeniden aciliyor. (Kalici izin: bir sonraki oturum acilisinda gecerli olur.)')
                self.open_conn()
            else:
                self.info(f'{dev}: izin verilemedi: {out}', 'error')
            QtCore.QTimer.singleShot(3000, lambda: setattr(self, '_perm_try', False))
        _bg(self, work, done)

    def info(self, text, level='info'):
        self.term.write_segments(self.fmt.text_line(time.time(), level, text),
                                 self.chk_scroll.isChecked())
        if self.log_file is not None and not self.log_bin:
            try:
                self.log_file.write(''.join(x[1] for x in self.log_fmt.text_line(time.time(), level, text)))
            except (OSError, ValueError):
                pass

    def update_stats(self):
        now = time.monotonic()
        t0, rx0, tx0 = self._rate_prev
        dt = max(1e-3, now - t0)
        self.rx_rate = (self.rx_total - rx0) / dt
        self.tx_rate = (self.tx_total - tx0) / dt
        self._rate_prev = (now, self.rx_total, self.tx_total)
        lb = self.fmt.labels
        hb = core.human_bytes
        self.lbl_stats.setText(
            f'<span style="color:#3DDC84"><b>{lb[RX].rstrip(">")}</b></span> {hb(self.rx_total)} '
            f'({hb(self.rx_rate)}/s) &nbsp;&nbsp; '
            f'<span style="color:#4FA8FF"><b>{lb[TX].rstrip(">")}</b></span> {hb(self.tx_total)} '
            f'({hb(self.tx_rate)}/s)' + (' &nbsp; <i>[kayit acik]</i>' if self.log_file else ''))

    def reset_counters(self):
        self.rx_total = self.tx_total = 0
        self._rate_prev = (time.monotonic(), 0, 0)
        self.update_stats()

    # ------------------------------------------------------------- gorunum
    def _fmt_changed(self, *_):
        self.fmt.mode = self.mode.currentData()
        self.fmt.timestamps = self.chk_ts.isChecked()
        self.fmt.ctrl_hex = self.chk_ctrl.isChecked()
        self.fmt.last_key = None
        if self.log_fmt is not None:
            self.log_fmt.mode = self.fmt.mode
            self.log_fmt.ctrl_hex = self.fmt.ctrl_hex
            self.log_fmt.last_key = None

    def clear(self):
        self.term.clear()
        self.fmt.reset()

    def save_view(self):
        path, _ = W.QFileDialog.getSaveFileName(self, 'Ekrani kaydet', self._default_name('txt'),
                                                'Metin (*.txt *.log);;Tum dosyalar (*)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(self.term.toPlainText())
                self.info(f'Ekran kaydedildi: {path}')
            except OSError as e:
                self.info(f'Kaydedilemedi: {e}', 'error')

    def _default_name(self, ext):
        return os.path.join(self.main.last_dir,
                            f'bysterm_{self.KIND}_{time.strftime("%Y%m%d_%H%M%S")}.{ext}')

    def toggle_log(self, on):
        if on:
            path, _ = W.QFileDialog.getSaveFileName(
                self, 'Kayit dosyasi', self._default_name('log'),
                'Metin kayit (*.log *.txt);;Ham RX baytlari (*.bin);;Tum dosyalar (*)')
            if not path:
                self.btn_log.blockSignals(True)
                self.btn_log.setChecked(False)
                self.btn_log.blockSignals(False)
                return
            self.main.last_dir = os.path.dirname(path)
            self.log_bin = path.lower().endswith('.bin')
            try:
                self.log_file = open(path, 'ab' if self.log_bin else 'a', encoding=None if self.log_bin else 'utf-8',
                                     buffering=1 << 16)
            except OSError as e:
                self.info(f'Kayit dosyasi acilamadi: {e}', 'error')
                self.btn_log.blockSignals(True)
                self.btn_log.setChecked(False)
                self.btn_log.blockSignals(False)
                return
            self.log_fmt = core.Formatter(self.fmt.mode, True, ctrl_hex=self.fmt.ctrl_hex,
                                          labels=self.fmt.labels)
            self.btn_log.setText('■ Kaydi durdur')
            self.btn_log.setStyleSheet('QPushButton { color: #cf222e; font-weight: bold; }')
            self.info(f'Kayit basladi: {path}')
        else:
            f, self.log_file = self.log_file, None
            if f:
                if not self.log_bin:
                    f.write('\n')
                f.close()
                self.info('Kayit durduruldu')
            self.btn_log.setText('● Kayit')
            self.btn_log.setStyleSheet('')
        self.update_stats()

    # ------------------------------------------------------------- gonderme
    def build_payload(self, text):
        if self.send_mode.currentText() == 'HEX':
            return core.parse_hex(text)
        data = core.parse_escapes(text) if self.chk_esc.isChecked() else text.encode('utf-8')
        data += (b'', b'\r', b'\n', b'\r\n')[max(0, self.eol.currentIndex())]
        return data

    def send_now(self, from_timer=False):
        t = self.transport
        if t is None or not self.connected:
            if not from_timer:
                self.info('Once baglanti acin', 'warn')
            return
        text = self.send_edit.currentText()
        try:
            data = self.build_payload(text)
        except ValueError as e:
            self.info(str(e), 'error')
            self.chk_repeat.setChecked(False)
            return
        if not data:
            return
        t.send(data, self.send_target())
        if not from_timer and text:
            i = self.send_edit.findText(text)
            if i >= 0:
                self.send_edit.removeItem(i)
            self.send_edit.insertItem(0, text)
            self.send_edit.setCurrentIndex(0)
            self.send_edit.lineEdit().selectAll()

    def _repeat_toggled(self, on):
        if on:
            self.repeat_timer.start(self.repeat_ms.value())
        else:
            self.repeat_timer.stop()

    def send_file(self):
        if not self.transport:
            return
        path, _ = W.QFileDialog.getOpenFileName(self, 'Gonderilecek dosya', self.main.last_dir)
        if not path:
            return
        try:
            size = os.path.getsize(path)
            if size > 64 * 1024 * 1024:
                raise OSError('dosya cok buyuk (>64 MB)')
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as e:
            self.info(f'Dosya okunamadi: {e}', 'error')
            return
        self.main.last_dir = os.path.dirname(path)
        self.info(f'Dosya gonderiliyor: {os.path.basename(path)} ({core.human_bytes(len(data))})')
        self.transport.send(data, self.send_target())

    # ------------------------------------------------------------- kapanis
    def shutdown(self):
        self.drain_timer.stop()
        self.repeat_timer.stop()
        if self.transport is not None:
            t, self.transport = self.transport, None
            t.close()
        if self.log_file:
            self.log_file.close()
            self.log_file = None


# =========================================================================== seri
class SerialSession(Session):
    KIND = 'serial'
    TITLE = 'Seri'

    def build_connection(self, row, col):
        self.ss = SerialSettings(self.main)
        row.addWidget(self.ss, 1)
        ctl = FlowLayout()
        self.chk_dtr = W.QCheckBox('DTR')
        self.chk_dtr.setChecked(True)
        self.chk_rts = W.QCheckBox('RTS')
        self.chk_rts.setChecked(True)
        self.chk_dtr.toggled.connect(lambda v: self._ctl('set_dtr', v))
        self.chk_rts.toggled.connect(lambda v: self._ctl('set_rts', v))
        self.btn_break = W.QPushButton('BREAK')
        self.btn_break.setToolTip('250 ms BREAK sinyali gonder')
        self.btn_break.clicked.connect(lambda: self._ctl('send_break', 0.25))
        ctl.addWidget(self.chk_dtr)
        ctl.addWidget(self.chk_rts)
        ctl.addWidget(self.btn_break)
        ctl.addSpacing(16)
        self.leds = {}
        for name in ('CTS', 'DSR', 'DCD', 'RI'):
            lb = W.QLabel(name)
            lb.setAlignment(qenum(Qt, 'AlignmentFlag.AlignCenter'))
            lb.setMinimumWidth(38)
            self.leds[name] = lb
            ctl.addWidget(lb)
        self._set_leds(None)
        ctl.addStretch(1)
        self.lbl_portinfo = W.QLabel()
        self.lbl_portinfo.setStyleSheet('color:#888')
        ctl.addWidget(self.lbl_portinfo)
        col.addLayout(ctl)
        self.ss.port.currentIndexChanged.connect(self._port_info)
        self.ss.port.editTextChanged.connect(self._port_info)
        self.modem_timer = QtCore.QTimer(self)
        self.modem_timer.timeout.connect(self._poll_modem)
        self._port_info()

    def _port_info(self, *_):
        i = self.ss.port.currentIndex()
        same = i >= 0 and self.ss.port.itemText(i) == self.ss.port.currentText()
        tip = self.ss.port.itemData(i, qenum(Qt, 'ItemDataRole.ToolTipRole')) if same else ''
        self.lbl_portinfo.setText((tip or '').replace('\n', '  |  ')[:160])

    def _ctl(self, fn, v):
        t = self.transport
        if t is not None and self.connected:
            try:
                getattr(t, fn)(v)
            except Exception as e:      # noqa: BLE001
                self.info(f'{fn}: {e}', 'warn')

    def _set_leds(self, lines):
        for name, lb in self.leds.items():
            on = bool(lines and lines.get(name))
            bg = '#3DDC84' if on else ('#30373E' if lines is not None else '#1A1E22')
            fg = '#fff' if on else '#999'
            lb.setStyleSheet(f'QLabel {{ background:{bg}; color:{fg}; border-radius:3px; padding:1px 4px; }}')

    def _poll_modem(self):
        t = self.transport
        self._set_leds(t.modem_lines() if (t and self.connected) else None)

    def make_transport(self):
        cfg = self.ss.config()
        cfg.dtr = self.chk_dtr.isChecked()
        cfg.rts = self.chk_rts.isChecked()
        return core.SerialTransport(cfg)

    def set_connected(self, on):
        super().set_connected(on)
        self.btn_break.setEnabled(on)
        if on:
            self.modem_timer.start(250)
        else:
            self.modem_timer.stop()
            self._set_leds(None)

    def set_inputs_enabled(self, en):
        self.ss.setEnabled(en)

    def tab_label(self):
        dev = self.ss.current_device()
        return f'Seri {os.path.basename(dev)}' if dev else 'Seri'

    def update_ports(self, ports):
        if not self.transport:
            self.ss.update_ports(ports)
            self._port_info()

    def save_settings(self, st):
        self.ss.save(st, self.KIND)

    def load_settings_into(self, st):
        self.ss.load(st, self.KIND)


# =========================================================================== TCP istemci
class TcpClientSession(Session):
    KIND = 'tcpc'
    TITLE = 'TCP Istemci'

    def build_connection(self, row, col):
        self.host = W.QLineEdit('127.0.0.1')
        self.host.setPlaceholderText('IP veya host adi')
        self.host.setMinimumWidth(160)
        self.port = W.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5000)
        self.host.returnPressed.connect(self.toggle_connection)
        row.addWidget(W.QLabel('Sunucu'))
        row.addWidget(self.host, 1)
        row.addWidget(W.QLabel('Port'))
        row.addWidget(self.port)

    def connect_text(self):
        return 'Baglan'

    def disconnect_text(self):
        return 'Baglantiyi kes'

    def make_transport(self):
        h = self.host.text().strip()
        if not h:
            raise ValueError('Sunucu adresi girin')
        return core.TcpClientTransport(h, self.port.value())

    def set_inputs_enabled(self, en):
        self.host.setEnabled(en)
        self.port.setEnabled(en)

    def tab_label(self):
        return f'TCP→ {self.host.text()}:{self.port.value()}'

    def save_settings(self, st):
        st.setValue('tcpc/host', self.host.text())
        st.setValue('tcpc/port', self.port.value())

    def load_settings_into(self, st):
        self.host.setText(st.value('tcpc/host', '127.0.0.1'))
        self.port.setValue(int(st.value('tcpc/port', 5000)))


# =========================================================================== TCP sunucu
class TcpServerSession(Session):
    KIND = 'tcps'
    TITLE = 'TCP Sunucu'

    def build_connection(self, row, col):
        self.bind = W.QComboBox()
        self.bind.setEditable(True)
        self.bind.addItems(['0.0.0.0'] + local_ips() + ['127.0.0.1', '::'])
        self.port = W.QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setValue(5000)
        row.addWidget(W.QLabel('Dinle'))
        row.addWidget(self.bind, 1)
        row.addWidget(W.QLabel('Port'))
        row.addWidget(self.port)
        ips = ', '.join(local_ips()) or '-'
        lb = W.QLabel(f'Bu bilgisayarin IP adresleri: {ips}')
        lb.setStyleSheet('color:#888')
        lb.setTextInteractionFlags(qenum(Qt, 'TextInteractionFlag.TextSelectableByMouse'))
        col.addWidget(lb)

    def build_side_panel(self):
        box = W.QGroupBox('Bagli istemciler')
        lay = W.QVBoxLayout(box)
        self.clients = W.QListWidget()
        self.clients.setSelectionMode(qenum(W.QAbstractItemView, 'SelectionMode.ExtendedSelection'))
        self.clients.setToolTip('Secim yoksa gonderim TUM istemcilere gider')
        lay.addWidget(self.clients, 1)
        self.lbl_target = W.QLabel('Hedef: tum istemciler')
        self.clients.itemSelectionChanged.connect(self._target_label)
        lay.addWidget(self.lbl_target)
        b = W.QHBoxLayout()
        self.btn_kick = W.QPushButton('Baglantiyi kes')
        self.btn_kick.clicked.connect(self._kick)
        btn_none = W.QPushButton('Secimi kaldir')
        btn_none.clicked.connect(self.clients.clearSelection)
        b.addWidget(self.btn_kick)
        b.addWidget(btn_none)
        lay.addLayout(b)
        box.setMinimumWidth(230)
        return box

    def _target_label(self):
        sel = self.send_target()
        self.lbl_target.setText(f'Hedef: {len(sel)} secili istemci' if sel else 'Hedef: tum istemciler')

    def _kick(self):
        t = self.transport
        if t:
            for p in self.send_target() or []:
                t.kick(p)

    def send_target(self):
        return [i.text() for i in self.clients.selectedItems()] or None

    def on_state(self, state, text):
        if state == 'client+':
            self.clients.addItem(text)
        elif state == 'client-':
            peer = text.split(' ')[0]
            for it in self.clients.findItems(peer, qenum(Qt, 'MatchFlag.MatchExactly')):
                self.clients.takeItem(self.clients.row(it))
        self.main.session_state_changed(self)

    def set_connected(self, on):
        if not on:
            self.clients.clear()
        super().set_connected(on)

    def connect_text(self):
        return 'Dinle'

    def disconnect_text(self):
        return 'Durdur'

    def make_transport(self):
        return core.TcpServerTransport(self.bind.currentText().strip(), self.port.value())

    def set_inputs_enabled(self, en):
        self.bind.setEnabled(en)
        self.port.setEnabled(en)

    def tab_label(self):
        n = self.clients.count() if self.connected else 0
        return f'TCP Sunucu :{self.port.value()}' + (f' ({n})' if n else '')

    def save_settings(self, st):
        st.setValue('tcps/bind', self.bind.currentText())
        st.setValue('tcps/port', self.port.value())

    def load_settings_into(self, st):
        self.bind.setCurrentText(st.value('tcps/bind', '0.0.0.0'))
        self.port.setValue(int(st.value('tcps/port', 5000)))


# =========================================================================== UDP
class UdpSession(Session):
    KIND = 'udp'
    TITLE = 'UDP'

    def build_connection(self, row, col):
        self.rhost = W.QLineEdit('127.0.0.1')
        self.rhost.setPlaceholderText('hedef IP (bos = sadece dinle)')
        self.rhost.setMinimumWidth(150)
        self.rport = W.QSpinBox()
        self.rport.setRange(0, 65535)
        self.rport.setValue(5001)
        self.lhost = W.QComboBox()
        self.lhost.setEditable(True)
        self.lhost.addItems(['0.0.0.0'] + local_ips() + ['127.0.0.1'])
        self.lport = W.QSpinBox()
        self.lport.setRange(0, 65535)
        self.lport.setValue(5000)
        self.lport.setToolTip('0 = sistem rastgele port secer')
        self.chk_bc = W.QCheckBox('Broadcast')
        self.chk_bc.setToolTip('255.255.255.255 / x.x.x.255 adreslerine gondermeye izin ver')
        row.addWidget(W.QLabel('Hedef'))
        row.addWidget(self.rhost, 1)
        row.addWidget(W.QLabel(':'))
        row.addWidget(self.rport)
        row.addSpacing(12)
        row.addWidget(W.QLabel('Yerel'))
        row.addWidget(self.lhost)
        row.addWidget(W.QLabel(':'))
        row.addWidget(self.lport)
        row.addWidget(self.chk_bc)
        ips = ', '.join(local_ips()) or '-'
        lb = W.QLabel(f'Bu bilgisayarin IP adresleri: {ips}')
        lb.setStyleSheet('color:#888')
        lb.setTextInteractionFlags(qenum(Qt, 'TextInteractionFlag.TextSelectableByMouse'))
        col.addWidget(lb)
        self.last_sender = None

    def build_send_extras(self, layout):
        self.chk_reply = W.QCheckBox('Son gonderene yanitla')
        self.chk_reply.setToolTip('Isaretliyse hedef, en son paket gelen adres:port olur')
        layout.addWidget(self.chk_reply)

    def connect_text(self):
        return 'Ac'

    def make_transport(self):
        rh = self.rhost.text().strip()
        return core.UdpTransport(self.lhost.currentText().strip(), self.lport.value(),
                                 rh, self.rport.value() if rh else 0, self.chk_bc.isChecked())

    def _drain_transport(self, t):
        # son gonderen adresini yakala
        for ev in list(t.events)[-200:]:
            if ev[0] == 'data' and ev[2] == RX:
                self.last_sender = ev[4]
        super()._drain_transport(t)

    def send_target(self):
        if self.chk_reply.isChecked() and self.last_sender:
            h, _, p = self.last_sender.rpartition(':')
            return (h, int(p))
        rh = self.rhost.text().strip()
        if rh and self.rport.value():
            return (rh, self.rport.value())
        return None

    def set_inputs_enabled(self, en):
        for w in (self.lhost, self.lport, self.chk_bc):
            w.setEnabled(en)

    def tab_label(self):
        return f'UDP :{self.lport.value()}'

    def save_settings(self, st):
        st.setValue('udp/rhost', self.rhost.text())
        st.setValue('udp/rport', self.rport.value())
        st.setValue('udp/lhost', self.lhost.currentText())
        st.setValue('udp/lport', self.lport.value())

    def load_settings_into(self, st):
        self.rhost.setText(st.value('udp/rhost', '127.0.0.1'))
        self.rport.setValue(int(st.value('udp/rport', 5001)))
        self.lhost.setCurrentText(st.value('udp/lhost', '0.0.0.0'))
        self.lport.setValue(int(st.value('udp/lport', 5000)))


# =========================================================================== seri izleme
class MonitorSession(Session):
    KIND = 'monitor'
    TITLE = 'Seri Izleme'
    HAS_SEND = False

    def build_connection(self, row, col):
        # --- mod secimi
        mrow = FlowLayout()
        self.mode_sel = W.QComboBox()
        if core.IS_WIN:
            self.mode_sel.addItem('Canli dinleme (USB-seri, Eltima gibi)', 'live')
        else:
            self.mode_sel.addItem('Canli dinleme (calisan uygulamayi izle)', 'live')
        self.mode_sel.addItem('Sanal port koprusu', 'bridge')
        self.mode_sel.addItem('Pasif donanim tap (2 port)', 'tap')
        if not (core.IS_LINUX or core.IS_WIN):
            self.mode_sel.model().item(0).setEnabled(False)   # macOS: canli dinleme yok
            self.mode_sel.setCurrentIndex(1)
        self.mode_sel.currentIndexChanged.connect(self._mode_changed)
        mrow.addWidget(W.QLabel('Yontem'))
        mrow.addWidget(self.mode_sel, 1)
        self.btn_rescan = W.QToolButton()
        self.btn_rescan.setText('⟳')
        self.btn_rescan.setToolTip('Seri port acmis uygulamalari yeniden tara')
        self.btn_rescan.clicked.connect(lambda: self._fill_procs())
        mrow.addWidget(self.btn_rescan)
        col.addLayout(mrow)

        # --- canli dinleme: surec listesi
        self.live_row = W.QWidget()
        lr = FlowLayout(self.live_row)
        self.proc = W.QComboBox()
        self.proc.setMinimumWidth(320)
        if core.IS_WIN:      # Windows: izlenecek COM portu (USB-seri cevirici) secilir
            self.proc.setToolTip('Izlenecek USB-seri port. Portu baska bir uygulama acmis olabilir; '
                                 'BYSTerm porta dokunmaz.')
            lr.addWidget(W.QLabel('Port'))
        else:
            lr.addWidget(W.QLabel('Uygulama'))
        lr.addWidget(self.proc, 1)
        col.addWidget(self.live_row)

        # Windows: USBPcap (imzali USB yakalama surucusu) durumu / kurulum
        self.usb_row = W.QWidget()
        ur = FlowLayout(self.usb_row)
        self.btn_usbpcap = W.QPushButton('USB dinleme surucusunu kur (USBPcap)')
        self.btn_usbpcap.clicked.connect(self._usbpcap_install)
        self.lbl_usbpcap = W.QLabel('')
        self.lbl_usbpcap.setStyleSheet('color:#8b929c')
        self.lbl_usbpcap.setWordWrap(True)
        ur.addWidget(self.btn_usbpcap)
        ur.addWidget(self.lbl_usbpcap, 1)
        col.addWidget(self.usb_row)
        self.usb_row.setVisible(False)

        # --- kopru / tap: gercek port + ayar
        self.ss = SerialSettings(self.main)
        self.ss.port.lineEdit().setPlaceholderText('Cihazin bagli oldugu GERCEK port')
        self.port_row = W.QWidget()
        pr = FlowLayout(self.port_row)
        pr.addWidget(W.QLabel('Gercek'))
        pr.addWidget(self.ss, 1)
        col.addWidget(self.port_row)

        self.virt_row = W.QWidget()
        r2 = FlowLayout(self.virt_row)
        self.virt = W.QComboBox()
        self.virt.setEditable(True)
        self.virt.setMinimumWidth(200)
        self.chk_follow = W.QCheckBox('Uygulamanin baud/format ayarini takip et')
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip('Diger uygulama sanal portu hangi baud ile acarsa gercek port '
                                   'de o baud\'a gecer (Linux/macOS)')
        self.lbl_v = W.QLabel('Sanal port')
        r2.addWidget(self.lbl_v)
        r2.addWidget(self.virt, 1)
        r2.addWidget(self.chk_follow)
        col.addWidget(self.virt_row)
        self.chk_passive = W.QCheckBox()      # geriye uyum (make_transport kullanir)
        self.chk_passive.setVisible(False)

        # Windows: com0com sanal port surucusu kur / cift olustur
        self.c0c_row = W.QWidget()
        cc = FlowLayout(self.c0c_row)
        self.btn_c0c = W.QPushButton('Sanal port surucusu kur (com0com)')
        self.btn_c0c.clicked.connect(self._c0c_install)
        self.btn_c0c_pair = W.QPushButton('Yeni sanal port cifti olustur')
        self.btn_c0c_pair.clicked.connect(self._c0c_pair)
        self.lbl_c0c = W.QLabel('')
        self.lbl_c0c.setStyleSheet('color:#8b929c')
        cc.addWidget(self.btn_c0c)
        cc.addWidget(self.btn_c0c_pair)
        cc.addWidget(self.lbl_c0c, 1)
        col.addWidget(self.c0c_row)
        self.c0c_row.setVisible(core.IS_WIN)

        self.help = W.QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet('color:#888')
        col.addWidget(self.help)
        self.chk_follow.setVisible(core.IS_POSIX)
        self._fill_virt(self.main.ports)
        self._fill_procs()
        self._mode_changed()

    def cur_mode(self):
        return self.mode_sel.currentData()

    def _mode_changed(self, *_):
        mode = self.cur_mode()
        self.chk_passive.setChecked(mode == 'tap')
        self.live_row.setVisible(mode == 'live')
        self.port_row.setVisible(mode != 'live')
        self.virt_row.setVisible(mode == 'bridge')
        self.usb_row.setVisible(core.IS_WIN and mode == 'live')
        self.btn_rescan.setToolTip('Portlari yeniden tara' if core.IS_WIN else
                                   'Seri port acmis uygulamalari yeniden tara')
        if mode == 'live' and core.IS_WIN:
            self._fill_procs()
            self._usbpcap_refresh()
            self.help.setText(
                'CANLI DINLEME (Windows, Eltima gibi): Portu BASKA bir uygulama acmisken bile o porttaki '
                'trafigi SANAL PORT OLMADAN gorursunuz; o uygulama hic degismez, BYSTerm porta dokunmaz. '
                'Gelen/giden veri, uygulamanin sectigi baud/format ve DTR/RTS degisiklikleri gorunur. '
                'USB-seri ceviriciler icindir (FTDI, CP210x, CH340, PL2303, Arduino/STM32/ESP32 gibi USB '
                'CDC). Bir kez ucretsiz USBPcap surucusu kurulur (Wireshark da kullanir, Microsoft imzali). '
                'Anakart uzerindeki yerlesik COM portlari icin "Sanal port koprusu" yontemini kullanin.')
        elif mode == 'live':
            self._fill_procs()
            self.help.setText(
                'CANLI DINLEME (Linux/Jetson): Portu BASKA bir uygulama acsa bile (orn. minicom, kendi '
                'programiniz), o uygulamanin seri trafigini SANAL PORT OLMADAN burada gorursunuz; izlenen '
                'uygulama hic degismez, veriye dokunulmaz (pasif). Listeden uygulamayi secip baslatin. '
                'Yetki gerekirse yonetici izni istenir.')
        elif mode == 'tap':
            self.lbl_v.setText('Ikinci port (B)')
            self.help.setText(
                'PASIF DONANIM TAP: Iki USB-seri cevirici; A = ustteki port, B = asagidaki ikinci port, '
                'RX uclari izlenen hattin TX ve RX\'ine baglanir. BYSTerm ikisini de sadece dinler. '
                'GND\'leri ortak baglayin.')
            self.virt_row.setVisible(True)
            self.lbl_v.setText('Ikinci port (B)')
            self.chk_follow.setVisible(False)
            self.virt.lineEdit().setPlaceholderText('ikinci gercek port (B)')
        else:
            self.lbl_v.setText('Sanal port')
            self.chk_follow.setVisible(core.IS_POSIX)
            self._passive_toggled(False)
        if hasattr(self, 'c0c_row'):
            self.c0c_row.setVisible(core.IS_WIN and mode == 'bridge')
            if core.IS_WIN and mode == 'bridge':
                self._c0c_refresh()

    def _fill_procs(self):
        if core.IS_WIN:
            return self._fill_usb_ports(self.main.ports)
        if not core.IS_LINUX or self.proc.view().isVisible():
            return
        cur = self.proc.currentData()
        self.proc.blockSignals(True)
        self.proc.clear()
        openers = core.list_serial_openers()
        for o in openers:
            for dev in o['devices']:
                short = o['cmd'] if len(o['cmd']) < 60 else o['cmd'][:57] + '...'
                self.proc.addItem(f'{dev}  ←  {o["name"]} (pid {o["pid"]})  {short}', (o['pid'], dev))
        if self.proc.count() == 0:
            self.proc.addItem('(seri port acmis uygulama bulunamadi — once o uygulamada portu acin)', None)
        idx = self.proc.findData(cur) if cur else -1
        self.proc.setCurrentIndex(max(0, idx))
        self.proc.blockSignals(False)

    def _fill_usb_ports(self, ports):
        if self.proc.view().isVisible():
            return
        cur = self.proc.currentData()
        self.proc.blockSignals(True)
        self.proc.clear()
        for p in ports:
            if p.vid is not None:        # sadece USB cihazlari
                self.proc.addItem(p.label, (p.device, p.vid, p.pid))
        for p in ports:
            if p.vid is None:
                self.proc.addItem(p.label + '  (USB degil)', (p.device, None, None))
        if self.proc.count() == 0:
            self.proc.addItem('(seri port bulunamadi — USB-seri ceviriciyi takin)', None)
        idx = -1
        if cur:
            for i in range(self.proc.count()):
                d = self.proc.itemData(i)
                if d and d[0] == cur[0]:
                    idx = i
                    break
        self.proc.setCurrentIndex(max(0, idx))
        self.proc.blockSignals(False)

    def _usbpcap_refresh(self):
        if not core.IS_WIN:
            return
        if usbsniff.usbpcap_installed():
            self.btn_usbpcap.setText('USBPcap kurulu ✓ (yeniden kur)')
            self.lbl_usbpcap.setText('Hazir. Yeni kurduysaniz Windows\'u bir kez yeniden baslatin.')
        else:
            self.btn_usbpcap.setText('USB dinleme surucusunu kur (USBPcap)')
            self.lbl_usbpcap.setText('Bir kez kurulur (ucretsiz, imzali). Kurulumdan sonra Windows yeniden '
                                     'baslatilmali.')

    def _usbpcap_install(self):
        self.btn_usbpcap.setEnabled(False)
        self.lbl_usbpcap.setText('USBPcap indiriliyor/kuruluyor...')
        _bg(self, usbsniff.usbpcap_install, self._usbpcap_done)

    def _usbpcap_done(self, res, err):
        self.btn_usbpcap.setEnabled(True)
        ok, msg = res if res else (False, str(err))
        self._usbpcap_refresh()
        self.lbl_usbpcap.setText(msg)
        if not ok and 'Iptal' not in msg:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(usbsniff.USBPCAP_PAGE))

    def _fill_virt(self, ports):
        if self.virt.view().isVisible():
            return
        cur = self.virt.currentText()
        self.virt.blockSignals(True)
        self.virt.clear()
        if core.IS_POSIX:
            self.virt.addItem('/tmp/ttyV0', '/tmp/ttyV0')
        for p in ports:
            self.virt.addItem(p.label, p.device)
        if cur:
            self.virt.setEditText(cur)
        elif not core.IS_POSIX:
            self.virt.setEditText('')
        self.virt.blockSignals(False)
        if not core.IS_POSIX:
            self.virt.lineEdit().setPlaceholderText('ornek: COM11 (com0com ciftinin BIR ucu)')

    def _c0c_refresh(self):
        if not core.IS_WIN:
            return
        pairs = net.com0com_pairs()
        if net.com0com_setupc():
            self.btn_c0c.setText('com0com kurulu ✓ (yeniden kur)')
            txt = ('Ciftler: ' + ', '.join(f'{a}↔{b}' for a, b in pairs)) if pairs else 'Henuz cift yok — olusturun.'
            self.lbl_c0c.setText(txt)
            self.btn_c0c_pair.setEnabled(True)
        else:
            self.btn_c0c.setText('Sanal port surucusu kur (com0com)')
            self.lbl_c0c.setText('Seri izleme icin bir kez kurulur (ucretsiz).')
            self.btn_c0c_pair.setEnabled(False)

    def _c0c_install(self):
        self.btn_c0c.setEnabled(False)
        self.lbl_c0c.setText('com0com indiriliyor/kuruluyor...')
        _bg(self, lambda: net.com0com_install(log=lambda m: None), self._c0c_done)

    def _c0c_done(self, res, err):
        self.btn_c0c.setEnabled(True)
        ok, msg = res if res else (False, str(err))
        self.lbl_c0c.setText(msg)
        if not ok and ('ZIP degil' in msg or 'erisil' in msg.lower()):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(net.COM0COM_PAGE))
        self._c0c_refresh()
        self._fill_virt(self.main.ports)

    def _c0c_pair(self):
        self.btn_c0c_pair.setEnabled(False)
        _bg(self, net.com0com_create_pair, self._c0c_paired)

    def _c0c_paired(self, res, err):
        self.btn_c0c_pair.setEnabled(True)
        ok, msg = res if res else (False, str(err))
        self.lbl_c0c.setText(msg)
        QtCore.QTimer.singleShot(1500, self._c0c_refresh)

    def _passive_toggled(self, on):
        self.lbl_v.setText('Ikinci port (B)' if on else 'Sanal port')
        self.chk_follow.setEnabled(not on)
        if on:
            self.help.setText(
                'PASIF DINLEME: Iki gercek port ayni baud ile acilir ve SADECE dinlenir. '
                'A = ustteki port (orn. cihazin TX hatti), B = bu port (orn. cihazin RX hatti). '
                'Ceviricilerin GND\'lerini hatta baglamayi unutmayin.')
        elif core.IS_POSIX:
            self.help.setText(
                'Nasil calisir: BYSTerm gercek portu acar ve bir SANAL port olusturur (yukaridaki yol, '
                'orn. /tmp/ttyV0). Izlemek istediginiz uygulamada gercek port yerine bu sanal portu acin; '
                'iki yondeki tum trafik burada gorunur ve oldugu gibi iletilir. '
                '(Gercek port zaten baska uygulamada aciksa once onu kapatin.)')
        else:
            self.help.setText(
                'Nasil calisir (Windows): Bir sanal null-modem cifti gerekir (ucretsiz com0com: '
                'orn. COM11<->COM12). BYSTerm gercek portu ve ciftin BIR ucunu (COM11) acar; '
                'izlediginiz uygulamada ciftin OBUR ucunu (COM12) acin. Trafik iki yonde iletilir ve gorunur.')

    def labels(self):
        if self.cur_mode() == 'tap':
            return {RX: 'A>', TX: 'B>'}
        return {RX: 'CIHAZ>', TX: 'UYGUL>'}

    def connect_text(self):
        return 'Izlemeyi baslat'

    def disconnect_text(self):
        return 'Durdur'

    def make_transport(self):
        if self.cur_mode() == 'live' and core.IS_WIN:
            d = self.proc.currentData()
            if not d:
                raise ValueError('Izlenecek USB-seri portu secin')
            dev, vid, pid = d
            if vid is None:
                raise ValueError(f'{dev} bir USB cihazi degil. Yerlesik COM portlari icin '
                                 '"Sanal port koprusu" yontemini kullanin.')
            return usbsniff.UsbSerialSniffer(dev, vid=vid, pid=pid)
        if self.cur_mode() == 'live':
            d = self.proc.currentData()
            if not d:
                raise ValueError('Izlenecek uygulamayi secin (seri port acmis bir surec)')
            pid, dev = d
            return core.SerialSniffer(pid, dev)
        cfg = self.ss.config()
        i = self.virt.currentIndex()
        txt = self.virt.currentText().strip()
        virt = (self.virt.itemData(i) if i >= 0 and self.virt.itemText(i) == txt else None) or \
            txt.split('  —  ')[0].split('  [')[0].strip()
        if not virt:
            raise ValueError('Sanal / ikinci port belirtin')
        if os.path.normcase(virt) == os.path.normcase(cfg.port):
            raise ValueError('Gercek port ile sanal/ikinci port ayni olamaz')
        return core.SerialBridge(cfg, virt, follow=self.chk_follow.isChecked(),
                                 passive=self.chk_passive.isChecked())

    def set_inputs_enabled(self, en):
        for w in (self.ss, self.virt, self.chk_follow, self.mode_sel, self.proc, self.btn_rescan):
            w.setEnabled(en)
        self.btn_usbpcap.setEnabled(en)

    def tab_label(self):
        if self.cur_mode() == 'live':
            d = self.proc.currentData()
            if not d:
                return tx('Seri Izleme')
            return tx('Dinle ') + (d[0] if core.IS_WIN else os.path.basename(d[1]))
        return tx('Izleme ') + os.path.basename(self.ss.current_device() or '')

    def update_ports(self, ports):
        if not self.transport:
            self.ss.update_ports(ports)
            self._fill_virt(ports)
            if self.cur_mode() == 'live':
                self._fill_procs()

    def save_settings(self, st):
        self.ss.save(st, self.KIND)
        st.setValue('monitor/virt', self.virt.currentText())

    def load_settings_into(self, st):
        self.ss.load(st, self.KIND)
        v = st.value('monitor/virt', '')
        if v:
            self.virt.setEditText(v)


# =========================================================================== ag araclari
class LineGraph(W.QWidget):
    """Basit canli cizgi grafik (ping RTT / iperf hizi). None = kayip (kirmizi cizgi)."""

    def __init__(self, unit='', color='#2DD4BF', maxpts=120, fmt='{:.1f}', parent=None):
        super().__init__(parent)
        self.unit, self.color, self.fmt = unit, color, fmt
        self.vals = collections.deque(maxlen=maxpts)
        self.title = ''
        self.setMinimumHeight(110)

    def add(self, v):
        self.vals.append(v)
        self.update()

    def clear(self):
        self.vals.clear()
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
        r = self.rect()
        p.fillRect(r, QtGui.QColor(term_colors()['bg']))
        L, T, R, B = 52, 18, r.width() - 8, r.height() - 8
        vals = list(self.vals)
        good = [v for v in vals if v is not None]
        top = max(good) * 1.2 if good else 1.0
        top = top or 1.0
        grid = '#30373E' if THEME['dark'] else '#d8dde2'
        p.setPen(QtGui.QPen(QtGui.QColor(grid), 1))
        f = p.font()
        f.setPointSize(max(7, f.pointSize() - 2))
        p.setFont(f)
        for i in range(5):
            y = T + (B - T) * i / 4.0
            p.setPen(QtGui.QPen(QtGui.QColor(grid), 1))
            p.drawLine(QtCore.QPointF(L, y), QtCore.QPointF(R, y))
            p.setPen(QtGui.QColor(term_colors()['hdr']))
            p.drawText(QtCore.QRectF(0, y - 8, L - 4, 16), qenum(Qt, 'AlignmentFlag.AlignRight') |
                       qenum(Qt, 'AlignmentFlag.AlignVCenter'), self.fmt.format(top * (4 - i) / 4.0))
        n = min(self.vals.maxlen, max(len(vals), 20))   # az noktayla da genis gorunsun
        step = (R - L) / float(max(1, n - 1))
        x0 = R - (len(vals) - 1) * step
        pts = []
        for i, v in enumerate(vals):
            x = x0 + i * step
            if v is None:
                if len(pts) > 1:
                    p.setPen(QtGui.QPen(QtGui.QColor(self.color), 2))
                    p.drawPolyline(QtGui.QPolygonF(pts))
                pts = []
                p.setPen(QtGui.QPen(QtGui.QColor(term_colors()['error']), 2))
                p.drawLine(QtCore.QPointF(x, B), QtCore.QPointF(x, B - 10))
                continue
            pts.append(QtCore.QPointF(x, B - (B - T) * v / top))
        if len(pts) > 1:
            p.setPen(QtGui.QPen(QtGui.QColor(self.color), 2))
            p.drawPolyline(QtGui.QPolygonF(pts))
        elif len(pts) == 1:
            p.setBrush(QtGui.QColor(self.color))
            p.drawEllipse(pts[0], 2, 2)
        p.setPen(QtGui.QColor(term_colors()['fg']))
        last = next((v for v in reversed(vals) if v is not None), None)
        txt = self.title + ('   son: ' + self.fmt.format(last) + ' ' + self.unit if last is not None else '')
        p.drawText(QtCore.QRectF(L, 1, R - L, 16), qenum(Qt, 'AlignmentFlag.AlignLeft'), txt)
        p.end()


def _bg(widget, fn, done):
    """fn'i arka planda calistir, sonucu GUI thread'inde done(sonuc, hata) ile ver."""
    box = {}

    def work():
        try:
            box['r'] = fn()
        except Exception as e:     # noqa: BLE001
            box['e'] = e
    th = threading.Thread(target=work, daemon=True)
    th.start()
    tm = QtCore.QTimer(widget)

    def poll():
        if not th.is_alive():
            tm.stop()
            tm.deleteLater()
            done(box.get('r'), box.get('e'))
    tm.timeout.connect(poll)
    tm.start(80)


def _ro_item(text, color=None, align_right=False):
    it = W.QTableWidgetItem(str(text))
    it.setFlags(qenum(Qt, 'ItemFlag.ItemIsSelectable') | qenum(Qt, 'ItemFlag.ItemIsEnabled'))
    if color:
        it.setForeground(QtGui.QBrush(QtGui.QColor(color)))
    if align_right:
        it.setTextAlignment(qenum(Qt, 'AlignmentFlag.AlignRight') | qenum(Qt, 'AlignmentFlag.AlignVCenter'))
    return it


def _setup_table(tbl, headers, stretch_col=None):
    tbl.setColumnCount(len(headers))
    tbl.setHorizontalHeaderLabels(headers)
    tbl.verticalHeader().setVisible(False)
    tbl.setSelectionBehavior(qenum(W.QAbstractItemView, 'SelectionBehavior.SelectRows'))
    tbl.setAlternatingRowColors(True)
    hh = tbl.horizontalHeader()
    for i in range(len(headers)):
        hh.setSectionResizeMode(i, qenum(W.QHeaderView, 'ResizeMode.ResizeToContents'))
    if stretch_col is not None:
        hh.setSectionResizeMode(stretch_col, qenum(W.QHeaderView, 'ResizeMode.Stretch'))


def source_ip_combo():
    cb = W.QComboBox()
    cb.setEditable(True)
    cb.addItem('Otomatik', '')
    for ip in local_ips():
        if not ip.startswith('127.'):
            cb.addItem(ip, ip)
    cb.setToolTip('Hangi ag kartindan cikilsin (birden fazla Ethernet varsa)')
    return cb


def combo_value(cb):
    i = cb.currentIndex()
    if i >= 0 and cb.itemText(i) == cb.currentText():
        return cb.itemData(i) or ''
    t = cb.currentText().strip()
    return '' if t.lower() in ('otomatik', tx('Otomatik').lower()) else t


class ToolTab(W.QWidget):
    """Ag araci sekmeleri icin ortak taban (MainWindow uyumu)."""
    KIND = 'tool'
    TITLE = 'Arac'
    transport = None

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.connected = False

    def tab_label(self):
        return self.TITLE

    def is_busy(self):
        return False

    def update_ports(self, ports):
        pass

    def store_settings(self):
        pass

    def shutdown(self):
        pass

    def set_running(self, on):
        self.connected = on
        self.main.session_state_changed(self)


# --------------------------------------------------------------------------- Ag ayarlari
COMMON_MASKS = ['255.255.255.0  (/24)', '255.255.0.0  (/16)', '255.0.0.0  (/8)', '255.255.255.128  (/25)',
                '255.255.255.192  (/26)', '255.255.255.240  (/28)', '255.255.254.0  (/23)', '255.255.252.0  (/22)']


class NetConfigTab(ToolTab):
    KIND = 'netcfg'
    TITLE = 'Ag Ayarlari'

    def __init__(self, main):
        super().__init__(main)
        self.ifaces = []
        self.cur = None
        root = W.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 4)

        top = W.QHBoxLayout()
        self.btn_refresh = W.QPushButton('⟳ Yenile')
        self.btn_refresh.clicked.connect(self.refresh)
        self.chk_virtual = W.QCheckBox('Sanal arayuzleri de goster (VPN, Docker, Hyper-V...)')
        self.chk_virtual.toggled.connect(self.refresh)
        self.lbl_admin = W.QLabel()
        self.btn_admin = W.QPushButton('Yonetici izni ver')
        self.btn_admin.clicked.connect(self.main.request_admin)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.chk_virtual)
        top.addStretch(1)
        top.addWidget(self.lbl_admin)
        top.addWidget(self.btn_admin)
        root.addLayout(top)

        split = W.QSplitter(qenum(Qt, 'Orientation.Vertical'))
        self.table = W.QTableWidget()
        _setup_table(self.table, ['Arayuz', 'Baglanti', 'Hiz', 'IPv4 adres(ler)', 'Gateway', 'Mod', 'DNS', 'MAC'], 3)
        self.table.itemSelectionChanged.connect(self._selected)
        split.addWidget(self.table)

        low = W.QWidget()
        hl = W.QHBoxLayout(low)
        hl.setContentsMargins(0, 0, 0, 0)

        # --- ayar formu
        self.box = W.QGroupBox('Secili arayuz')
        fl = W.QGridLayout(self.box)
        self.rb_dhcp = W.QRadioButton('Otomatik (DHCP)')
        self.rb_static = W.QRadioButton('Statik IP')
        self.rb_static.setChecked(True)
        self.rb_static.toggled.connect(self._mode_changed)
        self.ed_ip = W.QLineEdit()
        self.ed_ip.setPlaceholderText('192.168.1.100')
        self.cb_mask = W.QComboBox()
        self.cb_mask.setEditable(True)
        self.cb_mask.addItems(COMMON_MASKS)
        self.ed_gw = W.QLineEdit()
        self.ed_gw.setPlaceholderText('bos birakilabilir')
        self.ed_dns1 = W.QLineEdit()
        self.ed_dns1.setPlaceholderText('ornek 8.8.8.8 (bos olabilir)')
        self.ed_dns2 = W.QLineEdit()
        rx = QtCore.QRegularExpression(r'[0-9.]*') if hasattr(QtCore, 'QRegularExpression') else None
        if rx is not None and hasattr(QtGui, 'QRegularExpressionValidator'):
            for e in (self.ed_ip, self.ed_gw, self.ed_dns1, self.ed_dns2):
                e.setValidator(QtGui.QRegularExpressionValidator(rx, self))
        self.btn_apply = W.QPushButton('Uygula')
        self.btn_apply.setObjectName('primary')
        self.btn_apply.clicked.connect(lambda: self.apply('set'))
        self.btn_add = W.QPushButton('Ek IP olarak ekle')
        self.btn_add.setToolTip('Mevcut IP ve internet bozulmadan bu IP\'yi karta EK olarak ekler\n'
                                '(cihazin alt agina ulasmak icin en pratik yol)')
        self.btn_add.clicked.connect(lambda: self.apply('add'))
        self.btn_del = W.QPushButton('Ek IP\'yi sil')
        self.btn_del.setToolTip('Listeden secilen ek IP adresini karttan kaldir')
        self.btn_del.clicked.connect(self.delete_extra)
        self.cb_extra = W.QComboBox()
        self.cb_extra.setToolTip('Bu karttaki ek IP adresleri')
        r = 0
        fl.addWidget(self.rb_dhcp, r, 0)
        fl.addWidget(self.rb_static, r, 1)
        r += 1
        for lbl, w in (('IP adresi', self.ed_ip), ('Maske', self.cb_mask), ('Gateway', self.ed_gw),
                       ('DNS 1', self.ed_dns1), ('DNS 2', self.ed_dns2)):
            fl.addWidget(W.QLabel(lbl), r, 0)
            fl.addWidget(w, r, 1)
            r += 1
        bl = W.QHBoxLayout()
        bl.addWidget(self.btn_apply)
        bl.addWidget(self.btn_add)
        fl.addLayout(bl, r, 0, 1, 2)
        r += 1
        el = W.QHBoxLayout()
        el.addWidget(W.QLabel('Ek IP\'ler'))
        el.addWidget(self.cb_extra, 1)
        el.addWidget(self.btn_del)
        fl.addLayout(el, r, 0, 1, 2)
        fl.setRowStretch(r + 1, 1)
        fl.setVerticalSpacing(6)
        hl.addWidget(self.box, 3)

        # --- otomatik IP + profiller + gunluk
        right = W.QVBoxLayout()
        auto = W.QGroupBox('Otomatik IP: cihazin agina gec')
        al = W.QGridLayout(auto)
        self.ed_dev = W.QLineEdit()
        self.ed_dev.setPlaceholderText('cihazin IP\'si, ornek 192.168.10.50')
        self.btn_find = W.QPushButton('Bos IP bul')
        self.btn_find.clicked.connect(self.find_free)
        self.lbl_auto = W.QLabel('Cihazin IP\'sini yazin: ayni alt agda bos bir IP bulunup forma doldurulur.\n'
                                 'Sonra "Ek IP olarak ekle" (internet bozulmaz) veya "Uygula".')
        self.lbl_auto.setWordWrap(True)
        self.lbl_auto.setStyleSheet('color:#888')
        al.addWidget(self.ed_dev, 0, 0)
        al.addWidget(self.btn_find, 0, 1)
        al.addWidget(self.lbl_auto, 1, 0, 1, 2)
        right.addWidget(auto)

        prof = W.QGroupBox('Profiller (kayitli ayarlar)')
        pl = W.QHBoxLayout(prof)
        self.cb_prof = W.QComboBox()
        self.cb_prof.setMinimumWidth(160)
        b_load = W.QPushButton('Forma yukle')
        b_load.clicked.connect(self.load_profile)
        b_save = W.QPushButton('Formu kaydet...')
        b_save.clicked.connect(self.save_profile)
        b_delp = W.QPushButton('Sil')
        b_delp.clicked.connect(self.delete_profile)
        pl.addWidget(self.cb_prof, 1)
        for b in (b_load, b_save, b_delp):
            pl.addWidget(b)
        right.addWidget(prof)
        self.log = Terminal()
        self.log.setMaximumBlockCount(2000)
        self.log.setLineWrapMode(qenum(W.QPlainTextEdit, 'LineWrapMode.WidgetWidth'))
        right.addWidget(self.log, 1)
        hl.addLayout(right, 4)
        split.addWidget(low)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)

        self.fmt = core.Formatter()
        self._load_profiles()
        self.update_admin_label()
        self._mode_changed()
        self.refresh()

    # -- yardimcilar
    def say(self, text, level='info'):
        self.log.write_segments(self.fmt.text_line(time.time(), level, text))

    def update_admin_label(self):
        st = self.main.admin_state()
        self.lbl_admin.setText(st[1])
        self.btn_admin.setVisible(not st[0] and not net.IS_WIN)

    def _mode_changed(self, *_):
        st = self.rb_static.isChecked()
        for w in (self.ed_ip, self.cb_mask, self.ed_gw, self.ed_dns1, self.ed_dns2):
            w.setEnabled(st)

    def refresh(self, *_):
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText('Okunuyor...')
        inc = self.chk_virtual.isChecked()
        _bg(self, lambda: net.list_interfaces(include_virtual=inc), self._fill)

    def _fill(self, lst, err):
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText('⟳ Yenile')
        if err:
            self.say(f'Arayuzler okunamadi: {err}', 'error')
            return
        keep = self.cur.key if self.cur else None
        self.ifaces = lst or []
        t = self.table
        t.setRowCount(len(self.ifaces))
        sel = 0
        for r, it in enumerate(self.ifaces):
            up = '● bagli' if it.up else ('○ kablo yok' if it.up is False else '?')
            col = '#3DDC84' if it.up else '#888'
            mode = {True: 'DHCP', False: 'Statik', None: '-'}[it.dhcp]
            kind = {'wifi': ' (Wi-Fi)', 'virtual': ' (sanal)'}.get(it.kind, '')
            vals = [it.display + kind, up, f'{it.speed} Mbit/s' if it.speed else '-',
                    ', '.join(f'{a}/{p}' for a, p in it.addrs) or '-', it.gateway or '-', mode,
                    ', '.join(it.dns) or '-', it.mac or '-']
            for c, v in enumerate(vals):
                t.setItem(r, c, _ro_item(v, col if c == 1 else None))
            if it.desc:
                t.item(r, 0).setToolTip(it.desc)
            if it.key == keep:
                sel = r
        if self.ifaces:
            t.selectRow(sel)
        else:
            self.say('Ag arayuzu bulunamadi', 'warn')
        self.main.ifaces_changed(self.ifaces)

    def _selected(self):
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return
        it = self.ifaces[min(rows)]
        self.cur = it
        self.box.setTitle(f'Secili arayuz: {it.display}' + (f'   ({it.desc})' if it.desc and it.desc != it.display else ''))
        (self.rb_dhcp if it.dhcp else self.rb_static).setChecked(True)
        self.ed_ip.setText(it.ip)
        self.cb_mask.setCurrentText(net.prefix_to_mask(it.prefix) + f'  (/{it.prefix})' if it.addrs else COMMON_MASKS[0])
        self.ed_gw.setText(it.gateway)
        self.ed_dns1.setText(it.dns[0] if it.dns else '')
        self.ed_dns2.setText(it.dns[1] if len(it.dns) > 1 else '')
        self.cb_extra.clear()
        for a, p in it.addrs[1:]:
            self.cb_extra.addItem(f'{a}/{p}', (a, p))
        self.btn_del.setEnabled(self.cb_extra.count() > 0)

    def _mask_text(self):
        return self.cb_mask.currentText().split('(')[0].strip() or '24'

    # -- uygulama
    def apply(self, action, ip=None, mask=None):
        it = self.cur
        if it is None:
            self.say('Once listeden bir arayuz secin', 'warn')
            return
        mode = 'dhcp' if (self.rb_dhcp.isChecked() and action == 'set') else 'static'
        try:
            plan = net.make_plan(it, mode, ip or self.ed_ip.text().strip(), mask or self._mask_text(),
                                 self.ed_gw.text().strip() if action == 'set' else '',
                                 [self.ed_dns1.text().strip(), self.ed_dns2.text().strip()] if action == 'set' else [],
                                 action)
        except ValueError as e:
            self.say(str(e), 'error')
            return
        warn = ''
        if action == 'set' and it.gateway:
            warn = ('\n\nDIKKAT: Bu kart su an varsayilan ag gecidine (internet/uzak baglanti) sahip. '
                    'Degistirirseniz bu karttan yapilan baglantilar kopabilir. Interneti korumak icin '
                    '"Ek IP olarak ekle" kullanabilirsiniz.')
        msg = f'{plan.title}\n\nCalistirilacak komutlar:\n{plan.text()}' + \
              (f'\n\nNot: {plan.note}' if plan.note else '') + warn
        box = W.QMessageBox(self)
        box.setWindowTitle('Ag ayarini uygula')
        box.setIcon(qenum(W.QMessageBox, 'Icon.Warning' if warn else 'Icon.Question'))
        box.setText(msg)
        box.setStandardButtons(qenum(W.QMessageBox, 'StandardButton.Yes') | qenum(W.QMessageBox, 'StandardButton.No'))
        if qexec(box) != qenum(W.QMessageBox, 'StandardButton.Yes'):
            return
        self.say(f'Uygulaniyor: {plan.title}')
        for b in (self.btn_apply, self.btn_add, self.btn_del):
            b.setEnabled(False)
        _bg(self, lambda: net.apply_plan(plan), self._applied)

    def _applied(self, res, err):
        for b in (self.btn_apply, self.btn_add):
            b.setEnabled(True)
        self.btn_del.setEnabled(self.cb_extra.count() > 0)
        if err:
            self.say(f'Hata: {err}', 'error')
            return
        ok, out = res
        if out:
            self.say(out, 'info' if ok else 'error')
        self.say('Basarili. Liste yenileniyor (DHCP birkac saniye surebilir)...' if ok else 'BASARISIZ', 'info' if ok else 'error')
        QtCore.QTimer.singleShot(1500, self.refresh)
        QtCore.QTimer.singleShot(6000, self.refresh)

    def delete_extra(self):
        d = self.cb_extra.currentData()
        if d:
            self.apply('del', ip=d[0], mask=str(d[1]))

    # -- otomatik IP
    def find_free(self, device_ip=None):
        dev = (device_ip or self.ed_dev.text()).strip()
        if device_ip:
            self.ed_dev.setText(dev)
        if not net.is_ipv4(dev):
            self.say('Gecerli bir cihaz IP\'si girin', 'warn')
            return
        prefix = 24
        self.btn_find.setEnabled(False)
        self.lbl_auto.setText(f'{net.subnet_of(dev, prefix)} aginda bos IP araniyor...')
        mine = [a for i in self.ifaces for a, _ in i.addrs]
        _bg(self, lambda: (net.suggest_free_ip(dev, prefix, exclude=mine),
                           net.Pinger().ping(dev, 800).ok), lambda r, e: self._found(dev, prefix, r, e))

    def _found(self, dev, prefix, res, err):
        self.btn_find.setEnabled(True)
        if err or not res or not res[0]:
            self.lbl_auto.setText(f'Bos IP bulunamadi ({err or "alt ag dolu?"})')
            return
        ip, alive = res
        self.rb_static.setChecked(True)
        self.ed_ip.setText(ip)
        self.cb_mask.setCurrentText(f'{net.prefix_to_mask(prefix)}  (/{prefix})')
        self.ed_gw.setText('')
        self.lbl_auto.setText(f'Onerilen: {ip}/{prefix}  (cihaz {dev} ' +
                              ('su an pinge CEVAP VERIYOR' if alive else 'henuz cevap vermiyor; farkli alt agda oldugu icin normal') +
                              ').\n"Ek IP olarak ekle" ile internetiniz bozulmadan cihaza ulasabilirsiniz.')

    # -- profiller
    def _load_profiles(self):
        try:
            self.profiles = json.loads(self.main.settings.value('net/profiles', '[]') or '[]')
        except ValueError:
            self.profiles = []
        self.cb_prof.clear()
        for p in self.profiles:
            self.cb_prof.addItem(p.get('name', '?'))

    def save_profile(self):
        name, ok = W.QInputDialog.getText(self, 'Profil kaydet', 'Profil adi (ornek: Jetson agi):')
        if not ok or not name.strip():
            return
        p = {'name': name.strip(), 'mode': 'dhcp' if self.rb_dhcp.isChecked() else 'static',
             'ip': self.ed_ip.text().strip(), 'mask': self._mask_text(), 'gw': self.ed_gw.text().strip(),
             'dns1': self.ed_dns1.text().strip(), 'dns2': self.ed_dns2.text().strip()}
        self.profiles = [x for x in self.profiles if x.get('name') != p['name']] + [p]
        self.main.settings.setValue('net/profiles', json.dumps(self.profiles))
        self._load_profiles()
        self.cb_prof.setCurrentText(p['name'])
        self.say(f'Profil kaydedildi: {p["name"]}')

    def load_profile(self):
        i = self.cb_prof.currentIndex()
        if i < 0 or i >= len(self.profiles):
            return
        p = self.profiles[i]
        (self.rb_dhcp if p.get('mode') == 'dhcp' else self.rb_static).setChecked(True)
        self.ed_ip.setText(p.get('ip', ''))
        try:
            m, pre = net.parse_mask(p.get('mask', '24'))
            self.cb_mask.setCurrentText(f'{m}  (/{pre})')
        except ValueError:
            pass
        self.ed_gw.setText(p.get('gw', ''))
        self.ed_dns1.setText(p.get('dns1', ''))
        self.ed_dns2.setText(p.get('dns2', ''))
        self.say(f'Profil forma yuklendi: {p.get("name")}. Uygulamak icin "Uygula" veya "Ek IP olarak ekle".')

    def delete_profile(self):
        i = self.cb_prof.currentIndex()
        if 0 <= i < len(self.profiles):
            del self.profiles[i]
            self.main.settings.setValue('net/profiles', json.dumps(self.profiles))
            self._load_profiles()


# --------------------------------------------------------------------------- Ping
class PingTab(ToolTab):
    KIND = 'ping'
    TITLE = 'Ping'

    def __init__(self, main):
        super().__init__(main)
        self.workers = []
        self.rows = {}
        root = W.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 4)
        top = FlowLayout()
        self.ed_hosts = W.QLineEdit(main.settings.value('ping/hosts', '8.8.8.8') or '8.8.8.8')
        self.ed_hosts.setPlaceholderText('Hedef(ler): 192.168.1.10, 192.168.1.20, google.com')
        self.ed_hosts.setMinimumWidth(220)
        self.ed_hosts.returnPressed.connect(self.toggle)
        self.sp_int = W.QSpinBox()
        self.sp_int.setRange(10, 60000)
        self.sp_int.setValue(1000)
        self.sp_int.setSuffix(' ms')
        self.sp_to = W.QSpinBox()
        self.sp_to.setRange(50, 30000)
        self.sp_to.setValue(1000)
        self.sp_to.setSuffix(' ms')
        self.sp_size = W.QSpinBox()
        self.sp_size.setRange(0, 65000)
        self.sp_size.setValue(32)
        self.sp_size.setSuffix(' B')
        self.sp_cnt = W.QSpinBox()
        self.sp_cnt.setRange(0, 1000000)
        self.sp_cnt.setSpecialValueText('surekli')
        self.cb_src = source_ip_combo()
        self.btn = W.QPushButton('Baslat')
        self.btn.setMinimumWidth(100)
        self.btn.setObjectName('primary')
        self.btn.clicked.connect(self.toggle)
        top.addWidget(W.QLabel('Hedef'))
        top.addWidget(self.ed_hosts, 1)
        for lbl, w in (('Aralik', self.sp_int), ('Zaman asimi', self.sp_to), ('Boyut', self.sp_size),
                       ('Sayi', self.sp_cnt), ('Kaynak', self.cb_src)):
            top.addWidget(W.QLabel(lbl))
            top.addWidget(w)
        top.addWidget(self.btn)
        root.addLayout(top)

        self.table = W.QTableWidget()
        _setup_table(self.table, ['Hedef', 'IP', 'Durum', 'Son', 'Ort', 'Min', 'Max', 'Jitter', 'Kayip', 'Gonderilen'], 0)
        self.table.setMaximumHeight(170)
        self.table.itemSelectionChanged.connect(self._sel)
        root.addWidget(self.table)
        self.graph = LineGraph('ms', '#3DDC84', fmt='{:.1f}')
        root.addWidget(self.graph)
        opt = W.QHBoxLayout()
        self.chk_changes = W.QCheckBox('Gunluge sadece durum degisimlerini yaz (cevap geldi / kesildi)')
        self.chk_beep = W.QCheckBox('Durum degisince bip')
        b_clear = W.QPushButton('Temizle')
        opt.addWidget(self.chk_changes)
        opt.addWidget(self.chk_beep)
        opt.addStretch(1)
        opt.addWidget(b_clear)
        root.addLayout(opt)
        self.log = Terminal()
        self.log.setMaximumBlockCount(20000)
        root.addWidget(self.log, 1)
        b_clear.clicked.connect(self.clear)
        self.fmt = core.Formatter()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(100)
        self.sel_host = None

    def is_busy(self):
        return bool(self.workers)

    def tab_label(self):
        return 'Ping' + (f' ({len(self.workers)})' if self.workers else '')

    def add_host(self, host):
        cur = [h.strip() for h in self.ed_hosts.text().split(',') if h.strip()]
        if host not in cur:
            cur.append(host)
        self.ed_hosts.setText(', '.join(cur))

    def clear(self):
        self.log.clear()
        self.graph.clear()
        for st in self.rows.values():
            st['hist'].clear()

    def toggle(self):
        if self.workers:
            self.stop()
            return
        hosts = [h.strip() for h in self.ed_hosts.text().replace(';', ',').split(',') if h.strip()]
        if not hosts:
            return
        self.main.settings.setValue('ping/hosts', self.ed_hosts.text())
        self.rows = {}
        self.table.setRowCount(len(hosts))
        for r, h in enumerate(hosts):
            self.rows[h] = {'row': r, 'sent': 0, 'recv': 0, 'rtts': [], 'last_ok': None, 'min': None,
                            'max': None, 'sum': 0.0, 'jit': 0.0, 'prev': None,
                            'hist': collections.deque(maxlen=120)}
            for c, v in enumerate([h, '...', 'basliyor', '-', '-', '-', '-', '-', '-', '0']):
                self.table.setItem(r, c, _ro_item(v, align_right=c >= 3))
            w = net.PingWorker(h, self.sp_int.value() / 1000.0, self.sp_to.value(), self.sp_size.value(),
                               self.sp_cnt.value(), combo_value(self.cb_src))
            w.start()
            self.workers.append((h, w))
        self.sel_host = hosts[0]
        self.table.selectRow(0)
        self.graph.clear()
        self.graph.title = hosts[0]
        self.btn.setText('Durdur')
        self.set_running(True)

    def stop(self):
        for _, w in self.workers:
            w.stop()

    def _sel(self):
        rows = {i.row() for i in self.table.selectedItems()}
        for h, st in self.rows.items():
            if st['row'] in rows:
                self.sel_host = h
                self.graph.vals.clear()
                self.graph.vals.extend(st['hist'])
                self.graph.title = h
                self.graph.update()
                break

    def drain(self):
        if not self.workers:
            return
        segs = []
        alive = []
        for h, w in self.workers:
            st = self.rows[h]
            ended = False
            while w.events:
                ev = w.events.popleft()
                if ev[0] == 'info':
                    segs += self.fmt.text_line(ev[1], 'warn', ev[2])
                elif ev[0] == 'end':
                    ended = True
                elif ev[0] == 'reply':
                    segs += self._reply(h, st, w, ev[1], ev[2], ev[3])
            if not ended:
                alive.append((h, w))
        if segs:
            self.log.write_segments(segs)
        self.workers = alive
        if not alive:
            self.btn.setText('Baslat')
            self.set_running(False)

    def _reply(self, h, st, w, ts, seq, r):
        row = st['row']
        st['sent'] += 1
        segs = []
        changed = st['last_ok'] is not None and st['last_ok'] != r.ok
        first = st['last_ok'] is None
        st['last_ok'] = r.ok
        if r.ok:
            st['recv'] += 1
            st['sum'] += r.rtt
            st['min'] = r.rtt if st['min'] is None else min(st['min'], r.rtt)
            st['max'] = r.rtt if st['max'] is None else max(st['max'], r.rtt)
            if st['prev'] is not None:
                st['jit'] += (abs(r.rtt - st['prev']) - st['jit']) / 16.0
            st['prev'] = r.rtt
            st['hist'].append(r.rtt)
        else:
            st['hist'].append(None)
        if h == self.sel_host:
            self.graph.add(st['hist'][-1])
        loss = 100.0 * (st['sent'] - st['recv']) / st['sent']
        avg = st['sum'] / st['recv'] if st['recv'] else None
        f = lambda v: f'{v:.2f} ms' if v is not None else '-'   # noqa: E731
        vals = {1: w.ip or '?', 2: ('● cevap veriyor' if r.ok else '✖ ' + tx(r.err)),
                3: f(r.rtt if r.ok else None), 4: f(avg), 5: f(st['min']), 6: f(st['max']),
                7: f(st['jit'] if st['recv'] > 1 else None), 8: f'%{loss:.1f}',
                9: f'{st["sent"]} / {st["recv"]}'}
        for c, v in vals.items():
            it = self.table.item(row, c)
            if it:
                it.setText(v)
                if c == 2:
                    it.setForeground(QtGui.QBrush(QtGui.QColor(term_colors()[RX] if r.ok else term_colors()['error'])))
        if changed or first:
            if changed or not r.ok:
                txt = f'{h}: ▲ CEVAP VERMEYE BASLADI' if r.ok else f'{h}: ▼ CEVAP KESILDI ({tx(r.err)})'
                if first and not r.ok:
                    txt = f'{h}: cevap vermiyor ({tx(r.err)})'
                segs += self.fmt.text_line(ts, 'info' if r.ok else 'error', txt)
                if self.chk_beep.isChecked() and changed:
                    W.QApplication.beep()
        if not self.chk_changes.isChecked():
            stamp = time.strftime('%H:%M:%S', time.localtime(ts)) + f'.{int((ts % 1) * 1000):03d}'
            if r.ok:
                ttl = f'  TTL={r.ttl}' if r.ttl else ''
                segs.append(('hdr', f'{stamp} '))
                segs.append((RX, f'{h} ({w.ip})  {tx("sira")}={seq}  {tx("sure")}={r.rtt:.2f} ms{ttl}\n'))
            else:
                segs.append(('hdr', f'{stamp} '))
                segs.append(('error', f'{h}  {tx("sira")}={seq}  {tx(r.err)}\n'))
        return segs

    def shutdown(self):
        self.stop()


# --------------------------------------------------------------------------- IP tarama
class ScanTab(ToolTab):
    KIND = 'scan'
    TITLE = 'IP Tarama'

    def __init__(self, main):
        super().__init__(main)
        self.scanner = None
        self.rowmap = {}
        root = W.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 4)
        top = FlowLayout()
        self.cb_if = W.QComboBox()
        self.cb_if.setMinimumWidth(200)
        self.cb_if.currentIndexChanged.connect(self._if_changed)
        self.ed_range = W.QLineEdit('192.168.1.1-254')
        self.ed_range.setMinimumWidth(200)
        self.ed_range.setPlaceholderText('192.168.1.0/24  veya  192.168.1.1-254  veya  10.0.0.5-10.0.0.40')
        self.ed_range.returnPressed.connect(self.toggle)
        self.sp_to = W.QSpinBox()
        self.sp_to.setRange(100, 5000)
        self.sp_to.setValue(500)
        self.sp_to.setSuffix(' ms')
        self.btn = W.QPushButton('Tara')
        self.btn.setMinimumWidth(100)
        self.btn.setObjectName('primary')
        self.btn.clicked.connect(self.toggle)
        top.addWidget(W.QLabel('Ag karti'))
        top.addWidget(self.cb_if)
        top.addWidget(W.QLabel('IP araligi'))
        top.addWidget(self.ed_range, 1)
        top.addWidget(W.QLabel('Zaman asimi'))
        top.addWidget(self.sp_to)
        top.addWidget(self.btn)
        root.addLayout(top)
        self.prog = W.QProgressBar()
        self.prog.setTextVisible(True)
        root.addWidget(self.prog)
        self.table = W.QTableWidget()
        _setup_table(self.table, ['IP', 'Yanit suresi', 'TTL (tahmini sistem)', 'MAC', 'Host adi'], 4)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(qenum(Qt, 'ContextMenuPolicy.CustomContextMenu'))
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(lambda *_: self._copy())
        root.addWidget(self.table, 1)
        self.lbl = W.QLabel('Sag tik: kopyala / Ping\'e ekle / bu cihazin agina gec.  Cift tik: IP kopyala.')
        self.lbl.setStyleSheet('color:#888')
        root.addWidget(self.lbl)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(100)
        self.ifaces_changed(main.ifaces)

    def is_busy(self):
        return self.scanner is not None

    def ifaces_changed(self, ifaces):
        cur = self.cb_if.currentData()
        self.cb_if.blockSignals(True)
        self.cb_if.clear()
        for it in ifaces:
            for a, p in it.addrs:
                if not a.startswith('127.') and not a.startswith('169.254.'):
                    self.cb_if.addItem(f'{it.display}: {a}/{p}', (a, p))
        if self.cb_if.count() == 0:
            self.cb_if.addItem('(ag karti listesi bekleniyor)', None)
        i = self.cb_if.findData(cur) if cur else -1
        self.cb_if.setCurrentIndex(max(0, i))
        self.cb_if.blockSignals(False)
        if cur is None:
            self._if_changed()

    def _if_changed(self, *_):
        d = self.cb_if.currentData()
        if d:
            a, p = d
            p = max(p, 22)      # cok buyuk aglarda /22'den fazlasini varsayilan tarama
            self.ed_range.setText(net.subnet_of(a, p))

    def toggle(self):
        if self.scanner:
            self.scanner.stop()
            return
        try:
            ips = net.parse_range(self.ed_range.text())
        except ValueError as e:
            self.lbl.setText(str(e))
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.rowmap = {}
        d = self.cb_if.currentData()
        self.scanner = net.Scanner(ips, self.sp_to.value(), source=d[0] if d else '')
        self.scanner.start()
        self.prog.setRange(0, len(ips))
        self.prog.setValue(0)
        self.btn.setText('Durdur')
        self.lbl.setText(f'{len(ips)} adres taraniyor...')
        self.set_running(True)

    def _row(self, ip):
        if ip in self.rowmap:
            return self.rowmap[ip]
        r = self.table.rowCount()
        self.table.insertRow(r)
        it = _ro_item(ip)
        it.setData(qenum(Qt, 'ItemDataRole.UserRole'), net.ip2int(ip))
        self.table.setItem(r, 0, it)
        for c in range(1, 5):
            self.table.setItem(r, c, _ro_item('-'))
        self.rowmap[ip] = r
        return r

    def drain(self):
        s = self.scanner
        if not s:
            return
        while s.events:
            ev = s.events.popleft()
            k = ev[0]
            if k == 'host':
                r = self._row(ev[1])
                self.table.item(r, 1).setText(f'{ev[2]:.1f} ms' if ev[2] is not None else 'ping yok (ARP\'de var)')
                ttl = ev[3]
                guess = '' if ttl is None else (tx(' Linux/Jetson/cihaz') if ttl <= 64 else
                                                (' Windows' if ttl <= 128 else tx(' ag cihazi')))
                self.table.item(r, 2).setText(f'{ttl}{guess}' if ttl else '-')
            elif k == 'mac':
                self.table.item(self._row(ev[1]), 3).setText(ev[2])
            elif k == 'name':
                self.table.item(self._row(ev[1]), 4).setText(ev[2])
            elif k == 'progress':
                self.prog.setValue(ev[1])
            elif k == 'end':
                self.lbl.setText(f'Bitti: {ev[1]} cihaz bulundu ({ev[2]:.1f} sn). '
                                 f'Sag tik: kopyala / Ping\'e ekle / bu cihazin agina gec.')
                self.prog.setValue(self.prog.maximum())
                self.scanner = None
                self.btn.setText('Tara')
                self.table.setSortingEnabled(True)
                self.table.sortItems(0)
                self.set_running(False)

    def _ip_at(self, row=None):
        if row is None:
            rows = sorted({i.row() for i in self.table.selectedItems()})
            if not rows:
                return None
            row = rows[0]
        it = self.table.item(row, 0)
        return it.text() if it else None

    def _copy(self):
        ip = self._ip_at()
        if ip:
            W.QApplication.clipboard().setText(ip)
            self.main.statusBar().showMessage(f'Kopyalandi: {ip}', 3000)

    def _menu(self, pos):
        ip = self._ip_at()
        if not ip:
            return
        m = W.QMenu(self)
        m.addAction(f'{ip} kopyala').triggered.connect(self._copy)
        m.addAction('Ping sekmesine ekle').triggered.connect(lambda: self.main.send_to_ping(ip))
        m.addAction('iPerf hedefi yap').triggered.connect(lambda: self.main.send_to_iperf(ip))
        m.addAction('TCP Istemci hedefi yap').triggered.connect(lambda: self.main.send_to_tcp(ip))
        qexec_at(m, self.table.viewport().mapToGlobal(pos))

    def shutdown(self):
        if self.scanner:
            self.scanner.stop()


def qexec_at(menu, pos):
    return menu.exec(pos) if hasattr(menu, 'exec') else menu.exec_(pos)


# --------------------------------------------------------------------------- iPerf
class IperfTab(ToolTab):
    KIND = 'iperf'
    TITLE = 'iPerf'

    def __init__(self, main):
        super().__init__(main)
        self.job = None
        st = main.settings
        root = W.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 4)
        r1 = FlowLayout()
        self.rb_client = W.QRadioButton('Istemci (karsiya baglan)')
        self.rb_server = W.QRadioButton('Sunucu (bekle)')
        self.rb_client.setChecked(True)
        self.rb_client.toggled.connect(self._mode)
        self.ed_host = W.QLineEdit(st.value('iperf/host', '') or '')
        self.ed_host.setPlaceholderText('karsi cihazin IP\'si (orada: iperf3 -s)')
        self.ed_host.setMinimumWidth(180)
        self.ed_host.returnPressed.connect(self.toggle)
        self.sp_port = W.QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(5201)
        self.cb_proto = W.QComboBox()
        self.cb_proto.addItems(['TCP', 'UDP'])
        self.cb_proto.currentIndexChanged.connect(self._mode)
        self.sp_time = W.QSpinBox()
        self.sp_time.setRange(1, 86400)
        self.sp_time.setValue(10)
        self.sp_time.setSuffix(' sn')
        self.sp_par = W.QSpinBox()
        self.sp_par.setRange(1, 64)
        self.sp_par.setToolTip('Paralel akis sayisi (-P). 10G aglarda 4-8 deneyin.')
        self.chk_rev = W.QCheckBox('Ters yon (-R)')
        self.chk_rev.setToolTip('Isaretliyse karsi taraf GONDERIR, bu PC alir (indirme yonu)')
        self.sp_bw = W.QDoubleSpinBox()
        self.sp_bw.setRange(0, 100000)
        self.sp_bw.setDecimals(1)
        self.sp_bw.setSuffix(' Mbit/s')
        self.sp_bw.setSpecialValueText('sinirsiz')
        self.sp_bw.setToolTip('Hedef hiz (-b). TCP: 0 = sinirsiz. UDP: zorunlu (varsayilan 100).')
        self.cb_src = source_ip_combo()
        self.btn = W.QPushButton('Baslat')
        self.btn.setMinimumWidth(110)
        self.btn.setObjectName('primary')
        self.btn.clicked.connect(self.toggle)
        r1.addWidget(self.rb_client)
        r1.addWidget(self.rb_server)
        r1.addSpacing(10)
        self.lbl_host = W.QLabel('Sunucu')
        r1.addWidget(self.lbl_host)
        r1.addWidget(self.ed_host, 1)
        r1.addWidget(W.QLabel('Port'))
        r1.addWidget(self.sp_port)
        r1.addWidget(self.btn)
        root.addLayout(r1)
        r2 = FlowLayout()
        self.opts = []
        for lbl, w in (('Protokol', self.cb_proto), ('Sure', self.sp_time), ('Paralel', self.sp_par),
                       ('Hiz', self.sp_bw), ('Kaynak', self.cb_src)):
            l_ = W.QLabel(lbl)
            r2.addWidget(l_)
            r2.addWidget(w)
            self.opts += [l_, w]
        r2.addWidget(self.chk_rev)
        self.opts.append(self.chk_rev)
        r2.addStretch(1)
        root.addLayout(r2)
        self.lbl_help = W.QLabel()
        self.lbl_help.setWordWrap(True)
        self.lbl_help.setStyleSheet('color:#888')
        root.addWidget(self.lbl_help)

        big = W.QHBoxLayout()
        self.lbl_rate = W.QLabel('—')
        f = self.lbl_rate.font()
        f.setPointSize(f.pointSize() + 14)
        f.setBold(True)
        self.lbl_rate.setFont(f)
        self.lbl_rate.setStyleSheet(f'color:{ACC}')
        self.lbl_sum = W.QLabel('')
        self.lbl_sum.setStyleSheet('color:#888')
        big.addWidget(self.lbl_rate)
        big.addSpacing(20)
        big.addWidget(self.lbl_sum, 1)
        root.addLayout(big)
        self.graph = LineGraph('Mbit/s', ACC, fmt='{:.0f}')
        self.graph.setMinimumHeight(150)
        root.addWidget(self.graph)
        self.log = Terminal()
        self.log.setMaximumBlockCount(20000)
        root.addWidget(self.log, 1)
        self.fmt = core.Formatter()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(100)
        self._mode()

    def is_busy(self):
        return self.job is not None

    def tab_label(self):
        return 'iPerf' + (' ●' if self.job else '')

    def _mode(self, *_):
        cli = self.rb_client.isChecked()
        self.ed_host.setEnabled(cli)
        self.lbl_host.setText('Sunucu' if cli else 'Dinle')
        for w in self.opts:
            w.setEnabled(cli)
        if self.cb_proto.currentText() == 'UDP' and self.sp_bw.value() == 0:
            self.sp_bw.setValue(100)
        ips = ', '.join(ip for ip in local_ips() if not ip.startswith('127.')) or '-'
        if cli:
            self.lbl_help.setText('Karsi cihazda iperf3 sunucusu calismali:  iperf3 -s   (Jetson/Ubuntu: sudo apt install iperf3).  '
                                  'Ya da karsi PC\'de BYSTerm > iPerf > Sunucu.  Olculen, iki uc arasindan GERCEKTEN gecen net hizdir.')
        else:
            self.lbl_help.setText(f'Bu PC bekliyor. Karsi cihazdan:  iperf3 -c <bu PC IP> -p {self.sp_port.value()}   '
                                  f'(-R: bu PC gonderir, -u -b 100M: UDP).   Bu PC\'nin IP\'leri: {ips}')

    def say(self, text, level='info'):
        self.log.write_segments(self.fmt.text_line(time.time(), level, text))

    def toggle(self):
        if self.job:
            self.job.stop()
            self.btn.setEnabled(False)
            return
        self.graph.clear()
        self.lbl_rate.setText('—')
        self.lbl_sum.setText('')
        try:
            if self.rb_client.isChecked():
                host = self.ed_host.text().strip()
                if not host:
                    self.say('Sunucu adresini girin', 'warn')
                    return
                self.main.settings.setValue('iperf/host', host)
                udp = self.cb_proto.currentText() == 'UDP'
                bw = int(self.sp_bw.value() * 1e6)
                self.job = net.IperfClient(host, self.sp_port.value(), udp, self.sp_time.value(),
                                           self.sp_par.value(), self.chk_rev.isChecked(), bw,
                                           bind=combo_value(self.cb_src))
                self.graph.title = f'{host} {"UDP" if udp else "TCP"}' + (' (ters)' if self.chk_rev.isChecked() else '')
            else:
                self.job = net.IperfServer(self.sp_port.value(), '')
                self.graph.title = f'sunucu :{self.sp_port.value()}'
            self.job.start()
        except OSError as e:
            self.say(f'Baslatilamadi: {e}', 'error')
            self.job = None
            return
        self.btn.setText('Durdur')
        self.set_running(True)

    def drain(self):
        j = self.job
        if not j:
            return
        segs = []
        ended = False
        while j.events:
            ev = j.events.popleft()
            k = ev[0]
            if k == 'info':
                segs += self.fmt.text_line(ev[1], ev[2], ev[3])
            elif k == 'interval':
                _, a, b, nbytes, bps, extra = ev
                self.graph.add(bps / 1e6)
                self.lbl_rate.setText(net.fmt_rate(bps))
                line = f'[{a:6.1f} - {b:6.1f} sn]  {net.fmt_bytes(nbytes):>10}  {net.fmt_rate(bps):>14}'
                if extra:
                    line += f'   jitter {extra["jitter_ms"]:.3f} ms   kayip {extra["lost"]}/{extra["packets"]}'
                segs.append((RX, line + '\n'))
            elif k == 'result':
                r = ev[2]
                snd, rcv = tx('Gonderen'), tx('Alan')
                wd = max(len(snd), len(rcv))
                lines = [f'── {tx("SONUC")} ({r["elapsed"]:.1f} {tx("sn")}) ──',
                         f'  {snd:<{wd}}: {net.fmt_bytes(r["sent_bytes"]):>10}  {net.fmt_rate(r["sent_bps"]):>14}',
                         f'  {rcv:<{wd}}: {net.fmt_bytes(r["recv_bytes"]):>10}  {net.fmt_rate(r["recv_bps"]):>14}']
                summ = f'{tx("Alan tarafta olculen")}: {net.fmt_rate(r["recv_bps"])}'
                if r.get('udp'):
                    pk = r.get('packets') or 0
                    lost = r.get('lost', 0)
                    pct = 100.0 * lost / pk if pk else 0
                    lines.append(f'  {"UDP":<{wd}}: jitter {r.get("jitter_ms", 0):.3f} ms   {tx("kayip")} {lost}/{pk} (%{pct:.2f})')
                    summ += f'   jitter {r.get("jitter_ms", 0):.2f} ms   kayip %{pct:.2f}'
                segs.append(('info', '\n'.join(lines) + '\n'))
                self.lbl_rate.setText(net.fmt_rate(r['recv_bps']))
                self.lbl_sum.setText(summ)
            elif k == 'end':
                ended = True
        if segs:
            self.log.write_segments(segs)
        if ended:
            self.job = None
            self.btn.setText('Baslat')
            self.btn.setEnabled(True)
            self.set_running(False)

    def shutdown(self):
        if self.job:
            self.job.stop()


NET_TYPES = [NetConfigTab, PingTab, ScanTab, IperfTab]


SESSION_TYPES = [SerialSession, TcpClientSession, TcpServerSession, UdpSession, MonitorSession]
ALL_TYPES = SESSION_TYPES + NET_TYPES


# =========================================================================== ana pencere
# =========================================================================== tema / gorsel
BRAND1, BRAND2 = '#FFB547', '#FF5F6D'      # BYS aile simgesinde BYSTerm'in renk cifti
THEME = {'dark': True}

ACC, ACC2 = '#FFB547', '#FF5F6D'          # logo ile ayni amber->mercan vurgu (BYS ailesi)
_DARK = {      # ATOLYE: notr grafit
    'win': '#141719', 'panel': '#101214', 'base': '#1A1E22', 'alt': '#171B1F', 'btn': '#232930',
    'border': '#30373E', 'text': '#EDEEEF', 'muted': '#98A2AB', 'hover': '#2A323A', 'acc': ACC,
    'accdark': ACC2, 'sel': '#C8803A', 'pane_on': '#2A1E12', 'pane_off': '#1A1E22',
}
_LIGHT = {
    'win': '#f3f5f6', 'panel': '#e8ecee', 'base': '#ffffff', 'alt': '#f5f7f8', 'btn': '#ffffff',
    'border': '#c5ccd2', 'text': '#1d2125', 'muted': '#5f6a73', 'hover': '#e2e8ec', 'acc': '#D98B2B',
    'accdark': '#E0603A', 'sel': '#E89A4A', 'pane_on': '#fbe9d6', 'pane_off': '#e8ecee',
}


def _arrow_files(color):
    """Sayi kutusu / acilir liste oklari: QSS ucgen hilesi her surumde calismadigi icin PNG cizilir."""
    import tempfile
    d = os.path.join(tempfile.gettempdir(), f'bysterm_ui_{os.getpid()}')
    os.makedirs(d, exist_ok=True)
    out = {}
    for name, up in (('up', True), ('down', False)):
        pm = QtGui.QPixmap(10, 6)
        pm.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pm)
        p.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(color))
        pts = [QtCore.QPointF(0.5, 5.5), QtCore.QPointF(9.5, 5.5), QtCore.QPointF(5, 0.5)] if up else \
              [QtCore.QPointF(0.5, 0.5), QtCore.QPointF(9.5, 0.5), QtCore.QPointF(5, 5.5)]
        p.drawPolygon(QtGui.QPolygonF(pts))
        p.end()
        f = os.path.join(d, f'{name}_{color.strip("#")}.png')
        pm.save(f)
        out[name] = f.replace('\\', '/')
    return out


def apply_theme(app, dark=True):
    THEME['dark'] = dark
    c = _DARK if dark else _LIGHT
    app.setStyle('Fusion')
    pal = QtGui.QPalette()
    R = QtGui.QPalette
    roles = {'Window': c['win'], 'WindowText': c['text'], 'Base': c['base'], 'AlternateBase': c['alt'],
             'ToolTipBase': c['btn'], 'ToolTipText': c['text'], 'Text': c['text'], 'Button': c['btn'],
             'ButtonText': c['text'], 'BrightText': '#ffffff', 'Highlight': c['sel'],
             'HighlightedText': '#ffffff', 'Link': c['acc'], 'PlaceholderText': c['muted']}
    for name, col in roles.items():
        role = getattr(R, name, None) or getattr(getattr(R, 'ColorRole', R), name, None)
        if role is not None:
            pal.setColor(role, QtGui.QColor(col))
    dis = qenum(R, 'ColorGroup.Disabled')
    for name in ('Text', 'ButtonText', 'WindowText'):
        pal.setColor(dis, qenum(R, f'ColorRole.{name}'), QtGui.QColor(c['muted']))
    app.setPalette(pal)
    ar = _arrow_files(c['muted'])
    app.setStyleSheet(f"""
        QToolTip {{ background: {c['btn']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
        QPushButton {{ background: {c['btn']}; border: 1px solid {c['border']}; border-radius: 5px;
                      padding: 4px 9px; min-height: 18px; }}
        QPushButton:hover {{ border-color: {c['acc']}; background: {c['hover']}; }}
        QPushButton:pressed {{ background: {c['border']}; }}
        QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; }}
        QPushButton:checked {{ background: #2D1518; border-color: #F0555B; color: #F0555B; }}
        QPushButton#primary {{ color: #2A1705; font-weight: bold; border: none;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {c['acc']}, stop:1 {c['accdark']}); }}
        QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFC56E, stop:1 #FF7A86); }}
        QPushButton#primary:disabled {{ background: {c['border']}; color: {c['muted']}; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {c['base']}; border: 1px solid {c['border']};
            border-radius: 4px; padding: 2px 6px; min-height: 20px; }}
        QSpinBox, QDoubleSpinBox {{ padding-right: 16px; }}
        QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
            subcontrol-origin: border; width: 16px; border: none; background: transparent; }}
        QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; }}
        QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right; }}
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
        QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {c['hover']}; }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({ar['up']}); width: 10px; height: 6px; }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({ar['down']}); width: 10px; height: 6px; }}
        QComboBox::drop-down {{ border: none; width: 18px; }}
        QComboBox::down-arrow {{ image: url({ar['down']}); width: 10px; height: 6px; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c['acc']}; }}
        QComboBox QAbstractItemView {{ background: {c['base']}; selection-background-color: {c['sel']}; }}
        QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 14px; padding-top: 6px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['acc']}; font-weight: bold; }}
        QTableWidget, QListWidget {{ background: {c['base']}; alternate-background-color: {c['alt']};
            border: 1px solid {c['border']}; border-radius: 4px; gridline-color: {c['border']}; }}
        QHeaderView::section {{ background: {c['btn']}; border: none; border-right: 1px solid {c['border']};
            border-bottom: 1px solid {c['border']}; padding: 3px 6px; color: {c['muted']}; font-weight: bold; }}
        QSplitter::handle {{ background: {c['panel']}; }}
        QSplitter::handle:hover {{ background: {c['acc']}; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
        QScrollBar::handle {{ background: {c['border']}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
        QScrollBar::handle:hover {{ background: {c['muted']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QToolBar {{ background: {c['panel']}; border: none; border-bottom: 1px solid {c['border']}; spacing: 6px; padding: 3px; }}
        QStatusBar {{ background: {c['panel']}; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
        QMenu {{ background: {c['btn']}; border: 1px solid {c['border']}; padding: 4px; }}
        QMenu::item {{ padding: 5px 22px; border-radius: 4px; }}
        QMenu::item:selected {{ background: {c['sel']}; color: white; }}
        QProgressBar {{ border: 1px solid {c['border']}; border-radius: 4px; text-align: center; background: {c['base']}; }}
        QProgressBar::chunk {{ border-radius: 3px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c['acc']}, stop:1 {c['accdark']}); }}
        #sidebar {{ background: {c['panel']}; }}
        #sidebarBrand {{ color: {c['text']}; }}
    """)


def draw_bys_icon(g, s, c1=BRAND1, c2=BRAND2, pads=1.0, text=1.0, cursor=True, x0=0.0, y0=0.0):
    """BYS aile simgesi (ci/make_icon.py ile ayni tasarim). pads/text 0..1: acilis animasyonu icin."""
    import math

    def grad(a, b, angle, alpha=255):
        r = math.radians(angle)
        dx, dy = math.cos(r), math.sin(r)
        half = (abs(dx) + abs(dy)) * s / 2.0
        cx, cy = x0 + s / 2.0, y0 + s / 2.0
        gr = QtGui.QLinearGradient(cx - dx * half, cy - dy * half, cx + dx * half, cy + dy * half)
        ca, cb = QtGui.QColor(a), QtGui.QColor(b)
        ca.setAlpha(alpha)
        cb.setAlpha(alpha)
        gr.setColorAt(0, ca)
        gr.setColorAt(1, cb)
        return QtGui.QBrush(gr)

    def rr(x, y, w, h, r):
        p = QtGui.QPainterPath()
        d = max(0.5, min(2 * r, min(w, h)))
        p.addRoundedRect(QtCore.QRectF(x0 + x, y0 + y, w, h), d / 2, d / 2)
        return p
    m = max(1.0, s * 0.03)
    g.fillPath(rr(m, m, s - 2 * m, s - 2 * m, s * 0.22), grad('#1A2140', '#070A14', 60))
    acc = grad(c1, c2, 35)
    n, ln, pw, span = 6, s * 0.05, s * 0.034, s * 0.54
    st, edge = (s - span) / 2, s * 0.078
    total = n * 4
    lit = pads * total
    k = 0
    for side in range(4):
        for i in range(n):
            a = max(0.0, min(1.0, lit - k))
            k += 1
            if a <= 0:
                continue
            o = st + span * i / (n - 1) - pw / 2
            r = pw * 0.35
            brush = grad(c1, c2, 35, int(40 + 70 * a + (110 if a < 1 else 0) * (1 - a)))
            if side == 0:
                g.fillPath(rr(o, edge, pw, ln, r), brush)
            elif side == 1:
                g.fillPath(rr(s - edge - ln, o, ln, pw, r), brush)
            elif side == 2:
                g.fillPath(rr(s - o - pw, s - edge - ln, pw, ln, r), brush)
            else:
                g.fillPath(rr(edge, s - o - pw, ln, pw, r), brush)
    d = s * 0.05
    e = QtGui.QPainterPath()
    e.addEllipse(QtCore.QRectF(x0 + s * 0.17, y0 + s * 0.17, d, d))
    g.fillPath(e, acc)
    if text > 0:
        f = QtGui.QFont('DejaVu Sans Mono')
        f.setStyleHint(qenum(QtGui.QFont, 'StyleHint.Monospace'))
        f.setBold(True)
        f.setPixelSize(100)
        path = QtGui.QPainterPath()
        path.addText(0, 0, f, 'BYS')
        b = path.boundingRect()
        kk = min(s * 0.54 / b.width(), s * 0.27 / b.height())
        t = QtGui.QTransform()
        t.translate(x0 + s * 0.45, y0 + s * 0.50)
        t.scale(kk, kk)
        t.translate(-(b.x() + b.width() / 2), -(b.y() + b.height() / 2))
        tp = t.map(path)
        col = QtGui.QColor('#F4F6FF')
        col.setAlphaF(max(0.0, min(1.0, text)))
        g.fillPath(tp, QtGui.QBrush(col))
        if cursor:
            bb = tp.boundingRect()
            g.fillPath(rr(bb.right() - x0 + s * 0.035, bb.bottom() - y0 - s * 0.05, s * 0.115, s * 0.05, s * 0.012), acc)


def tool_icon(kind, size=18):
    """Sol paneldeki arac simgeleri (elle cizilir, her sistemde ayni)."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtGui.QColor(0, 0, 0, 0))
    p = QtGui.QPainter(pm)
    p.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
    gr = QtGui.QLinearGradient(0, 0, size, size)
    gr.setColorAt(0, QtGui.QColor(ACC))
    gr.setColorAt(1, QtGui.QColor(ACC2))
    pen = QtGui.QPen(QtGui.QBrush(gr), 1.6)
    p.setPen(pen)
    s = float(size)
    F = QtCore.QPointF
    R = QtCore.QRectF
    if kind == 'serial':          # DB9 konnektor
        path = QtGui.QPainterPath()
        path.moveTo(2, 5)
        path.lineTo(s - 2, 5)
        path.lineTo(s - 4, s - 5)
        path.lineTo(4, s - 5)
        path.closeSubpath()
        p.drawPath(path)
        p.setBrush(QtGui.QBrush(gr))
        for x in (5.5, 9, 12.5):
            p.drawEllipse(F(x * s / 18, 8.2 * s / 18), 0.9, 0.9)
        for x in (7.2, 10.8):
            p.drawEllipse(F(x * s / 18, 11 * s / 18), 0.9, 0.9)
    elif kind == 'tcpc':          # baglanti oku
        p.drawEllipse(F(4, s / 2), 2.2, 2.2)
        p.drawLine(F(7, s / 2), F(s - 3, s / 2))
        p.drawLine(F(s - 7, s / 2 - 4), F(s - 3, s / 2))
        p.drawLine(F(s - 7, s / 2 + 4), F(s - 3, s / 2))
    elif kind == 'tcps':          # sunucu
        for i in range(3):
            p.drawRoundedRect(R(3, 2.5 + i * 4.6, s - 6, 3.6), 1, 1)
            p.drawPoint(F(s - 6, 4.3 + i * 4.6))
    elif kind == 'udp':           # yayin
        p.setBrush(QtGui.QBrush(gr))
        p.drawEllipse(F(4, s - 4), 1.6, 1.6)
        p.setBrush(QtCore.Qt.NoBrush)
        for rad in (6, 10, 14):
            p.drawArc(R(4 - rad, s - 4 - rad, 2 * rad, 2 * rad), 0, 90 * 16)
    elif kind == 'monitor':       # goz
        path = QtGui.QPainterPath()
        path.moveTo(2, s / 2)
        path.quadTo(s / 2, 2, s - 2, s / 2)
        path.quadTo(s / 2, s - 2, 2, s / 2)
        p.drawPath(path)
        p.setBrush(QtGui.QBrush(gr))
        p.drawEllipse(F(s / 2, s / 2), 2.4, 2.4)
    elif kind == 'netcfg':        # ethernet jaki
        path = QtGui.QPainterPath()
        path.moveTo(3, 4)
        path.lineTo(s - 3, 4)
        path.lineTo(s - 3, s - 6)
        path.lineTo(s - 6, s - 6)
        path.lineTo(s - 6, s - 3)
        path.lineTo(6, s - 3)
        path.lineTo(6, s - 6)
        path.lineTo(3, s - 6)
        path.closeSubpath()
        p.drawPath(path)
        for x in range(4):
            p.drawLine(F(6 + x * 2, 6.5), F(6 + x * 2, 9))
    elif kind == 'ping':          # radar
        p.setBrush(QtGui.QBrush(gr))
        p.drawEllipse(F(s / 2, s / 2), 1.8, 1.8)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(F(s / 2, s / 2), 4.5, 4.5)
        p.drawEllipse(F(s / 2, s / 2), 7.5, 7.5)
    elif kind == 'scan':          # buyutec
        p.drawEllipse(F(7.5, 7.5), 4.6, 4.6)
        p.setPen(QtGui.QPen(QtGui.QBrush(gr), 2.4))
        p.drawLine(F(11, 11), F(s - 2.5, s - 2.5))
    elif kind == 'iperf':         # hiz gostergesi
        p.drawArc(R(2, 4, s - 4, s - 4), 0, 180 * 16)
        p.drawLine(F(s / 2, s / 2 + 2), F(s - 5, 6))
        p.setBrush(QtGui.QBrush(gr))
        p.drawEllipse(F(s / 2, s / 2 + 2), 1.6, 1.6)
    p.end()
    return QtGui.QIcon(pm)


class Splash(W.QWidget):
    """Acilis animasyonu: cip pedleri sirayla yanar, BYS_ belirir, imlec yanip soner, ad kayarak gelir."""
    DUR = 1.9

    def __init__(self, on_done):
        flags = qenum(Qt, 'WindowType.FramelessWindowHint') | qenum(Qt, 'WindowType.SplashScreen') | \
            qenum(Qt, 'WindowType.WindowStaysOnTopHint')
        super().__init__(None, flags)
        self.setAttribute(qenum(Qt, 'WidgetAttribute.WA_TranslucentBackground'))
        self.resize(560, 330)
        scr = W.QApplication.primaryScreen() if hasattr(W.QApplication, 'primaryScreen') else None
        if scr is not None:
            geo = scr.availableGeometry()
            self.move(geo.center() - self.rect().center())
        self.on_done = on_done
        self.t0 = time.monotonic()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    def _tick(self):
        t = time.monotonic() - self.t0
        if t >= self.DUR:
            self.timer.stop()
            self.close()
            self.on_done()
            return
        self.setWindowOpacity(1.0 if t < self.DUR - 0.35 else max(0.0, (self.DUR - t) / 0.35))
        self.update()

    def paintEvent(self, ev):
        import math
        t = time.monotonic() - self.t0
        p = QtGui.QPainter(self)
        p.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
        r = QtCore.QRectF(self.rect()).adjusted(6, 6, -6, -6)
        bg = QtGui.QLinearGradient(r.topLeft(), r.bottomRight())
        bg.setColorAt(0, QtGui.QColor('#1d2233'))
        bg.setColorAt(1, QtGui.QColor('#0b0d14'))
        path = QtGui.QPainterPath()
        path.addRoundedRect(r, 18, 18)
        p.fillPath(path, QtGui.QBrush(bg))
        # yumusak isik halesi
        glow = QtGui.QRadialGradient(QtCore.QPointF(r.left() + 140, r.center().y()), 170)
        cg = QtGui.QColor(BRAND2)
        cg.setAlpha(int(70 * min(1.0, t / 0.8)))
        glow.setColorAt(0, cg)
        glow.setColorAt(1, QtGui.QColor(0, 0, 0, 0))
        p.fillPath(path, QtGui.QBrush(glow))
        pen = QtGui.QPen(QtGui.QColor('#2c3245'), 1)
        p.setPen(pen)
        p.drawPath(path)
        # simge: buyuyerek gelir (ease-out-back)
        u = min(1.0, t / 0.55)
        sc = 1 + 2.2 * (u - 1) ** 3 + 1.2 * (u - 1) ** 2 if u < 1 else 1.0
        size = 170 * max(0.05, sc)
        cx, cy = r.left() + 140, r.center().y()
        draw_bys_icon(p, size, pads=max(0.0, (t - 0.25) / 0.7), text=max(0.0, (t - 0.55) / 0.3),
                      cursor=(int(t * 3.2) % 2 == 0) or t < 0.9, x0=cx - size / 2, y0=cy - size / 2)
        # yazi: soldan kayarak + belirerek
        a = max(0.0, min(1.0, (t - 0.6) / 0.45))
        dx = 30 * (1 - a) ** 2
        f = p.font()
        f.setPixelSize(46)
        f.setBold(True)
        p.setFont(f)
        tg = QtGui.QLinearGradient(QtCore.QPointF(260, 0), QtCore.QPointF(520, 0))
        c1, c2 = QtGui.QColor(BRAND1), QtGui.QColor(BRAND2)
        c1.setAlphaF(a)
        c2.setAlphaF(a)
        tg.setColorAt(0, c1)
        tg.setColorAt(1, c2)
        p.setPen(QtGui.QPen(QtGui.QBrush(tg), 1))
        p.drawText(QtCore.QRectF(258 + dx, cy - 62, 300, 60), qenum(Qt, 'AlignmentFlag.AlignLeft') |
                   qenum(Qt, 'AlignmentFlag.AlignBottom'), APP_NAME)
        f.setPixelSize(14)
        f.setBold(False)
        p.setFont(f)
        col = QtGui.QColor('#c9d1d9')
        col.setAlphaF(max(0.0, min(1.0, (t - 0.85) / 0.4)))
        p.setPen(col)
        p.drawText(QtCore.QRectF(260 + dx, cy + 4, 300, 22), qenum(Qt, 'AlignmentFlag.AlignLeft'),
                   tr('Serial · TCP · UDP · Network toolkit'))
        col2 = QtGui.QColor('#6e7681')
        col2.setAlphaF(col.alphaF())
        p.setPen(col2)
        p.drawText(QtCore.QRectF(260 + dx, cy + 28, 300, 20), qenum(Qt, 'AlignmentFlag.AlignLeft'),
                   f'v{APP_VERSION}  ·  Burak Yurtsever')
        # alt ilerleme cizgisi: kayan isik
        y = r.bottom() - 26
        p.setPen(QtCore.Qt.NoPen)
        track = QtCore.QRectF(r.left() + 40, y, r.width() - 80, 3)
        p.fillRect(track, QtGui.QColor('#232838'))
        prog = min(1.0, t / (self.DUR - 0.35))
        bar = QtCore.QRectF(track.left(), y, track.width() * (1 - (1 - prog) ** 2), 3)
        bg2 = QtGui.QLinearGradient(bar.topLeft(), bar.topRight())
        bg2.setColorAt(0, QtGui.QColor(BRAND1))
        bg2.setColorAt(1, QtGui.QColor(BRAND2))
        p.fillRect(bar, QtGui.QBrush(bg2))
        sh = (t * 1.4) % 1.0
        shine = QtGui.QRadialGradient(QtCore.QPointF(track.left() + track.width() * sh, y + 1.5), 40)
        shine.setColorAt(0, QtGui.QColor(255, 255, 255, 110))
        shine.setColorAt(1, QtGui.QColor(255, 255, 255, 0))
        p.fillRect(bar, QtGui.QBrush(shine))
        p.end()
        del math


ABOUT_TEXT = ('BYSTerm is a fast test and monitoring tool for embedded and network developers. '
              'It brings serial ports, TCP, UDP, serial traffic monitoring, network settings, ping, '
              'IP scanning and iperf3 speed tests together in one window with side-by-side panes. '
              'It stays responsive at high data rates and runs on Windows, macOS, Linux and NVIDIA Jetson '
              'without any installation.')


class AboutDialog(W.QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle(f'{tr("About")} {APP_NAME}')
        self.setMinimumWidth(560)
        lay = W.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        top = W.QHBoxLayout()
        icon = W.QLabel()
        pm = QtGui.QPixmap(112, 112)
        pm.fill(QtGui.QColor(0, 0, 0, 0))
        pp = QtGui.QPainter(pm)
        pp.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
        draw_bys_icon(pp, 112)
        pp.end()
        icon.setPixmap(pm)
        top.addWidget(icon)
        top.addSpacing(14)
        tl = W.QVBoxLayout()
        name = W.QLabel(f'<span style="font-size:30px; font-weight:800; color:{BRAND1}">BYS</span>'
                        f'<span style="font-size:30px; font-weight:800; color:{BRAND2}">Term</span>'
                        f'&nbsp;&nbsp;<span style="color:#8b929c; font-size:14px">v{APP_VERSION}</span>')
        tl.addWidget(name)
        sub = W.QLabel(tr('Serial · TCP · UDP · Network toolkit'))
        sub.setStyleSheet('color:#8b929c; font-size: 13px;')
        tl.addWidget(sub)
        tl.addStretch(1)
        top.addLayout(tl, 1)
        lay.addLayout(top)
        lay.addSpacing(10)
        txt = W.QLabel(tx(ABOUT_TEXT))
        txt.setWordWrap(True)
        txt.setStyleSheet('font-size: 13px; line-height: 140%;')
        lay.addWidget(txt)
        lay.addSpacing(10)
        url = f'https://github.com/{upd.REPO}'
        info = W.QLabel(
            f'<table cellspacing="4">'
            f'<tr><td style="color:#8b929c">{tr("Developer")}</td><td>&nbsp;&nbsp;<b>Burak Yurtsever</b></td></tr>'
            f'<tr><td style="color:#8b929c">{tr("Source code & releases")}</td>'
            f'<td>&nbsp;&nbsp;<a style="color:{BRAND1}" href="{url}">{url.replace("https://", "")}</a></td></tr>'
            f'</table>')
        info.setOpenExternalLinks(True)
        info.setTextInteractionFlags(qenum(Qt, 'TextInteractionFlag.TextBrowserInteraction'))
        lay.addWidget(info)
        lay.addSpacing(8)
        bl = W.QHBoxLayout()
        b_upd = W.QPushButton(tr('Check for updates now'))
        b_upd.clicked.connect(lambda: main.check_updates(manual=True))
        b_close = W.QPushButton(tr('Close'))
        b_close.setObjectName('primary')
        b_close.clicked.connect(self.accept)
        bl.addWidget(b_upd)
        bl.addStretch(1)
        bl.addWidget(b_close)
        lay.addLayout(bl)


class UpdateDialog(W.QDialog):
    """Yeni surum bildirimi + indirme/kurulum ilerlemesi."""

    def __init__(self, main, rel):
        super().__init__(main)
        self.main, self.rel = main, rel
        self.setWindowTitle(tr('Update available'))
        self.setMinimumWidth(520)
        lay = W.QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        head = W.QLabel('<span style="font-size:17px; font-weight:bold">' +
                        tr('A new version of BYSTerm is available: {new}  (you have {cur})').format(
                            new=f'<span style="color:{BRAND1}">v{rel["version"]}</span>', cur=f'v{APP_VERSION}') +
                        '</span>')
        head.setWordWrap(True)
        lay.addWidget(head)
        notes = rel.get('notes', '').strip()
        if notes:
            lay.addWidget(W.QLabel(f'<b>{tr("What&#39;s new:")}</b>'.replace('&#39;', "'")))
            box = W.QPlainTextEdit(notes[:4000])
            box.setReadOnly(True)
            box.setMaximumHeight(170)
            lay.addWidget(box)
        self.prog = W.QProgressBar()
        self.prog.hide()
        lay.addWidget(self.prog)
        self.status = W.QLabel('')
        self.status.setStyleSheet('color:#8b929c')
        lay.addWidget(self.status)
        bl = W.QHBoxLayout()
        self.b_skip = W.QPushButton(tr('Skip this version'))
        self.b_later = W.QPushButton(tr('Later'))
        self.b_now = W.QPushButton(tr('Update now'))
        self.b_now.setObjectName('primary')
        bl.addWidget(self.b_skip)
        bl.addStretch(1)
        bl.addWidget(self.b_later)
        bl.addWidget(self.b_now)
        lay.addLayout(bl)
        self.b_skip.clicked.connect(self._skip)
        self.b_later.clicked.connect(self.reject)
        self.b_now.clicked.connect(self._update)
        self._cancel = False
        self._state = {}

    def _skip(self):
        self.main.settings.setValue('skip_version', self.rel['version'])
        self.reject()

    def _open_page(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.rel.get('page') or upd.PAGE))

    def _update(self):
        if not upd.is_frozen():
            self.status.setText(tr('BYSTerm is running from source; opening the release page.'))
            self._open_page()
            return
        name = upd.asset_name()
        url = self.rel['assets'].get(name)
        if not url:
            self.status.setText(tr('No download for this system was found in the release. Opening the release page.'))
            self._open_page()
            return
        for b in (self.b_now, self.b_skip):
            b.setEnabled(False)
        self.b_later.setText(tr('Cancel'))
        self.b_later.clicked.disconnect()
        self.b_later.clicked.connect(lambda: setattr(self, '_cancel', True))
        self.prog.show()
        self.prog.setRange(0, 0)
        self.status.setText(tr('Downloading {name}...').format(name=name))
        st = self._state
        import tempfile

        def work():
            fd, tmp = tempfile.mkstemp(prefix='bysterm_', suffix='_' + name,
                                       dir=os.path.dirname(upd.install_target()) if upd.IS_WIN else None)
            os.close(fd)

            def prog(done, total):
                st['done'], st['total'] = done, total
            upd.download(url, tmp, prog, lambda: self._cancel)
            return upd.install(tmp)
        t = QtCore.QTimer(self)

        def tick():
            if st.get('total'):
                self.prog.setRange(0, st['total'])
                self.prog.setValue(st.get('done', 0))
        t.timeout.connect(tick)
        t.start(100)

        def done(cmd, err):
            t.stop()
            if err:
                msg = tr('No write permission for {path}. Download the new version manually from the release page.').format(
                    path=err) if isinstance(err, PermissionError) else str(err)
                if 'cancelled' in str(err):
                    self.reject()
                    return
                W.QMessageBox.warning(self, tr('Update failed'), msg)
                self._open_page()
                self.reject()
                return
            W.QMessageBox.information(self, APP_NAME, tr('The update was installed. BYSTerm will now restart.'))
            try:
                subprocess.Popen(cmd, close_fds=True)
            finally:
                self.main.close()
                W.QApplication.instance().quit()
        _bg(self, work, done)


# =========================================================================== Terminator tarzi calisma alani
def _icon(kind, color='#c9d1d9', size=16):
    """Basliktaki kucuk dugme ikonlari — her sistemde ayni gorunsun diye elle cizilir."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtGui.QColor(0, 0, 0, 0))
    p = QtGui.QPainter(pm)
    p.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
    pen = QtGui.QPen(QtGui.QColor(color), 1.6)
    p.setPen(pen)
    r = QtCore.QRectF(2.5, 3.5, size - 5, size - 7)
    if kind in ('hsplit', 'vsplit', 'zoom', 'unzoom'):
        p.drawRoundedRect(r, 2, 2)
    if kind == 'hsplit':      # yan yana
        p.drawLine(QtCore.QPointF(size / 2.0, r.top()), QtCore.QPointF(size / 2.0, r.bottom()))
    elif kind == 'vsplit':    # alt alta
        p.drawLine(QtCore.QPointF(r.left(), size / 2.0), QtCore.QPointF(r.right(), size / 2.0))
    elif kind == 'zoom':
        p.fillRect(QtCore.QRectF(r.left() + 2, r.top() + 2, r.width() - 4, r.height() - 4), QtGui.QColor(color))
    elif kind == 'unzoom':
        p.drawRect(QtCore.QRectF(r.left() + 3, r.top() + 2.5, r.width() - 6, r.height() - 5))
    elif kind == 'close':
        p.drawLine(QtCore.QPointF(4, 4), QtCore.QPointF(size - 4, size - 4))
        p.drawLine(QtCore.QPointF(size - 4, 4), QtCore.QPointF(4, size - 4))
    elif kind == 'plus':
        p.setPen(QtGui.QPen(QtGui.QColor(color), 2))
        p.drawLine(QtCore.QPointF(size / 2.0, 3), QtCore.QPointF(size / 2.0, size - 3))
        p.drawLine(QtCore.QPointF(3, size / 2.0), QtCore.QPointF(size - 3, size / 2.0))
    p.end()
    return QtGui.QIcon(pm)


def _gpos(ev):
    """Qt5/Qt6: fare olayinin ekran konumu (QPoint)."""
    if hasattr(ev, 'globalPosition'):
        return ev.globalPosition().toPoint()
    return ev.globalPos()


class PaneHeader(W.QWidget):
    """Bolme basligi: tutup surukleyince bolme tasinir (Qt surukle-birak sistemi KULLANILMAZ;
    fare dogrudan izlenir — etiketler fare olayini yutmaz, her platformda ayni calisir)."""

    def __init__(self, pane):
        super().__init__()
        self.pane = pane
        self._press = None
        self._dragging = False
        self.setCursor(QtGui.QCursor(qenum(Qt, 'CursorShape.OpenHandCursor')))

    def mousePressEvent(self, ev):
        if ev.button() == qenum(Qt, 'MouseButton.LeftButton'):
            self._press = _gpos(ev)
            self._dragging = False
            self.pane.ws.set_active(self.pane)
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._press is None:
            return
        g = _gpos(ev)
        if not self._dragging and (g - self._press).manhattanLength() > 10:
            if len(self.pane.ws.panes()) < 2:
                return
            self._dragging = True
            self.setCursor(QtGui.QCursor(qenum(Qt, 'CursorShape.ClosedHandCursor')))
            self.pane.ws.begin_drag(self.pane)
        if self._dragging:
            self.pane.ws.drag_move(g)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._dragging:
            self.pane.ws.end_drag(_gpos(ev))
        self._press = None
        self._dragging = False
        self.setCursor(QtGui.QCursor(qenum(Qt, 'CursorShape.OpenHandCursor')))
        ev.accept()


class DropOverlay(W.QWidget):
    """Surukleme sirasinda hedef bolmede 'buraya duser' alanini gosteren yari saydam katman
    (her seyin ustunde; fareyi engellemez)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(qenum(Qt, 'WidgetAttribute.WA_TransparentForMouseEvents'))
        self.label = ''
        self.hide()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
        r = QtCore.QRectF(self.rect()).adjusted(2, 2, -2, -2)
        col = QtGui.QColor(ACC)
        col.setAlpha(70)
        path = QtGui.QPainterPath()
        path.addRoundedRect(r, 8, 8)
        p.fillPath(path, col)
        p.setPen(QtGui.QPen(QtGui.QColor(ACC), 2.5))
        p.drawPath(path)
        if self.label:
            f = p.font()
            f.setPointSize(f.pointSize() + 3)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QtGui.QColor('#ffffff'))
            p.drawText(r, qenum(Qt, 'AlignmentFlag.AlignCenter'), self.label)
        p.end()


class Pane(W.QFrame):
    """Tek bir oturumu (Seri, TCP, Ping...) tutan bolme: baslik cubugu + icerik."""

    def __init__(self, ws, session=None):
        super().__init__()
        self.ws = ws
        self.session = None
        self.setObjectName('pane')
        lay = W.QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self.header = PaneHeader(self)
        self.header.setObjectName('paneHeader')
        self.header.setAttribute(qenum(Qt, 'WidgetAttribute.WA_StyledBackground'))
        hl = W.QHBoxLayout(self.header)
        hl.setContentsMargins(8, 2, 4, 2)
        hl.setSpacing(2)
        self.title = W.QLabel('')
        self.title.setObjectName('paneTitle')
        self.title.setToolTip('Başlığı tutup sürükleyerek pencereyi taşıyın')
        self.title.setAttribute(qenum(Qt, 'WidgetAttribute.WA_TransparentForMouseEvents'))
        hl.addWidget(self.title, 1)
        self.btns = {}
        for key, tip, fn in (('hsplit', 'Yana bol  (Ctrl+Shift+E)', lambda: ws.split_pane(self, 'h')),
                             ('vsplit', 'Alta bol  (Ctrl+Shift+O)', lambda: ws.split_pane(self, 'v')),
                             ('zoom', 'Tam ekran / geri al  (Ctrl+Shift+X)', lambda: ws.toggle_zoom(self)),
                             ('close', 'Kapat  (Ctrl+Shift+W)', lambda: ws.close_pane(self))):
            b = W.QToolButton()
            b.setIcon(_icon(key))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(fn)
            hl.addWidget(b)
            self.btns[key] = b
        lay.addWidget(self.header)
        self.scroll = W.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(qenum(W.QFrame, 'Shape.NoFrame'))
        lay.addWidget(self.scroll, 1)
        self.body = W.QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.placeholder = W.QLabel('Bos bolme\n\nSoldaki listeden bir arac secin\n'
                                    '(adina tiklayin: burada acilir,  + : yanina yeni bolme)')
        self.placeholder.setAlignment(qenum(Qt, 'AlignmentFlag.AlignCenter'))
        self.placeholder.setStyleSheet('color:#888; font-size: 13px;')
        self.scroll.setWidget(self.placeholder)
        if session is not None:
            self.set_session(session)
        self.set_active(False)

    def set_session(self, s):
        """Oturumu bu bolmeye koy; onceki oturumu dondur (arka planda yasamaya devam eder)."""
        old = self.session
        cur = self.scroll.takeWidget()
        if cur is not None and cur is not old:
            cur.setParent(self)
            cur.hide()
        if old is not None:
            old.hide()
            old.setParent(self.ws.holder)
        self.session = s
        if s is not None:
            self.scroll.setWidget(s)
            s.show()
        else:
            self.scroll.setWidget(self.placeholder)
            self.placeholder.show()
        self.refresh_title()
        return old

    def refresh_title(self):
        s = self.session
        if s is None:
            self.title.setText('<span style="color:#888">(bos)</span>')
            return
        dot = '<span style="color:#3DDC84">●</span> ' if s.connected else ''
        self.title.setText(f'{dot}<b>{tx(s.tab_label())}</b>')

    def set_active(self, on):
        self.setProperty('active', 'true' if on else 'false')
        dark = THEME['dark'] if 'THEME' in globals() else True
        on_hdr, off_hdr = ('#2A1E12', '#1A1E22') if dark else ('#fbe9d6', '#e9ebef')
        on_tx, off_tx = ('#EDEEEF', '#98A2AB') if dark else ('#1d2125', '#5f6a73')
        off_bd = '#30373E' if dark else '#c5ccd2'
        self.setStyleSheet(
            '#pane { border: 1px solid %s; }'
            '#paneHeader { background: %s; }'
            '#paneTitle { color: %s; }' % (
                (ACC, on_hdr, on_tx) if on else (off_bd, off_hdr, off_tx)))

    # -- surukle-birak: hedef bolge
    def _zone_at(self, pos):
        w, h = max(1, self.width()), max(1, self.height())
        x, y = pos.x(), pos.y()
        dl, dr, dt, db = x / w, (w - x) / w, y / h, (h - y) / h
        m = min(dl, dr, dt, db)
        return [z for z, d in (('left', dl), ('right', dr), ('top', dt), ('bottom', db)) if d == m][0]

    def zone_rect(self, zone):
        r = self.rect()
        w, h = r.width(), r.height()
        return {'left': QtCore.QRect(0, 0, w // 2, h), 'right': QtCore.QRect(w // 2, 0, w - w // 2, h),
                'top': QtCore.QRect(0, 0, w, h // 2), 'bottom': QtCore.QRect(0, h // 2, w, h - h // 2)}[zone]


class Workspace(W.QWidget):
    """Bolmeler agaci: ic ice QSplitter'lar. Bolme ekle / kapat / tam ekran / dolas."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.holder = W.QWidget(self)      # gorunmeyen (arka plan) oturumlarin evi
        self.holder.hide()
        self.lay = W.QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.root = None
        self.active = None
        self.zoomed = None
        self._set_root(Pane(self))
        self.set_active(self.root)
        self.overlay = DropOverlay(self)
        self._drag_src = None
        self._drag_tgt = None

    # -- agac yardimcilari
    def _set_root(self, w):
        if self.root is not None:
            self.lay.removeWidget(self.root)
        self.root = w
        self.lay.addWidget(w)
        w.show()

    def _replace(self, old, new):
        par = old.parentWidget()
        if isinstance(par, W.QSplitter):
            idx = par.indexOf(old)
            sizes = par.sizes()
            old.setParent(None)
            par.insertWidget(idx, new)
            par.setSizes(sizes)
        else:
            self.lay.removeWidget(old)
            old.setParent(None)
            self.root = new
            self.lay.addWidget(new)
        new.show()

    def panes(self, w=None):
        w = self.root if w is None else w
        if isinstance(w, Pane):
            return [w]
        out = []
        if isinstance(w, W.QSplitter):
            for i in range(w.count()):
                out += self.panes(w.widget(i))
        return out

    def pane_of(self, session):
        for p in self.panes():
            if p.session is session:
                return p
        return None

    def set_active(self, pane):
        if pane is self.active or pane is None:
            return
        if self.active is not None:
            try:
                self.active.set_active(False)
            except RuntimeError:
                pass
        self.active = pane
        pane.set_active(True)
        self.main.workspace_changed()

    # -- islemler
    def split_pane(self, pane, orient=None, session=None):
        """pane'i bol, yeni bolmeye session'i (yoksa bos) koy. orient: 'h' yan yana, 'v' alt alta."""
        if self.zoomed is not None:
            self.toggle_zoom(self.zoomed)
        if orient is None:     # Terminator gibi: uzun kenar yonunde bol
            ms = pane.minimumSizeHint()

            def fits(p, o):    # iki yarisi da asgari boyutun ustunde kalir mi (pencere ekrandan tasmasin)
                return p.width() >= 2 * max(ms.width(), 160) + 5 if o == 'h' else \
                    p.height() >= 2 * max(ms.height(), 120) + 5
            if not (fits(pane, 'h') or fits(pane, 'v')):
                big = max(self.panes(), key=lambda p: p.width() * p.height())
                if fits(big, 'h') or fits(big, 'v'):
                    pane = big    # secili bolme cok kucuk -> en buyuk bolmenin yanina ac
            orient = 'h' if pane.width() >= pane.height() * 1.2 else 'v'
            alt = 'v' if orient == 'h' else 'h'
            if not fits(pane, orient) and fits(pane, alt):
                orient = alt
        qo = qenum(Qt, 'Orientation.Horizontal' if orient == 'h' else 'Orientation.Vertical')
        new = Pane(self, session)
        par = pane.parentWidget()
        if isinstance(par, W.QSplitter) and par.orientation() == qo:
            idx = par.indexOf(pane)
            par.insertWidget(idx + 1, new)
            n = par.count()
            tot = sum(par.sizes()) or (par.width() if orient == 'h' else par.height())
            par.setSizes([tot // n] * n)
        else:
            sp = W.QSplitter(qo)
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(5)
            self._replace(pane, sp)
            sp.addWidget(pane)
            sp.addWidget(new)
            tot = pane.width() if orient == 'h' else pane.height()
            sp.setSizes([max(1, tot // 2)] * 2)
            pane.show()
        new.show()
        self.set_active(new)
        return new

    # -- fareyle tasima (PaneHeader cagirir)
    def begin_drag(self, pane):
        if self.zoomed is not None:
            self.toggle_zoom(self.zoomed)
        self._drag_src = pane
        self._drag_tgt = None

    def _pane_at(self, gpos):
        # geometriyle bul (widgetAt bazi platformlarda/ustte pencere varken guvenilmez)
        for p in self.panes():
            if p.isVisible() and p.rect().contains(p.mapFromGlobal(gpos)):
                return p
        return None

    def drag_move(self, gpos):
        tgt = self._pane_at(gpos)
        if tgt is None or tgt is self._drag_src:
            self._drag_tgt = None
            self.overlay.hide()
            return
        zone = tgt._zone_at(tgt.mapFromGlobal(gpos))
        self._drag_tgt = (tgt, zone)
        zr = tgt.zone_rect(zone)
        top_left = tgt.mapTo(self, zr.topLeft())
        self.overlay.setGeometry(QtCore.QRect(top_left, zr.size()))
        self.overlay.label = {'left': '◀ Sola', 'right': 'Sağa ▶', 'top': '▲ Üste', 'bottom': '▼ Alta'}[zone]
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.update()

    def end_drag(self, gpos):
        self.overlay.hide()
        src, tgt = self._drag_src, self._drag_tgt
        self._drag_src = self._drag_tgt = None
        if src is not None and tgt is not None:
            self.move_pane(src, tgt[0], tgt[1])

    def _detach_pane(self, pane):
        """Bolmeyi agactan cikar (oturumu YOK ETMEDEN). Agaci sadelestirir."""
        par = pane.parentWidget()
        pane.setParent(None)
        pane.deleteLater()
        if isinstance(par, W.QSplitter) and par.count() == 1:
            child = par.widget(0)
            self._replace(par, child)
            par.deleteLater()

    def move_pane(self, src, dst, side):
        """src bolmesini dst'nin yanina (side: left/right/top/bottom/center) tasir."""
        if src is dst or self.zoomed is not None:
            return
        sess = src.session
        src.set_session(None)                 # oturumu koru (holder'a gider)
        self._detach_pane(src)
        if side in ('center', None):
            side = 'right'
        qo = qenum(Qt, 'Orientation.Horizontal' if side in ('left', 'right') else 'Orientation.Vertical')
        after = side in ('right', 'bottom')
        new = Pane(self, None)
        par = dst.parentWidget()
        if isinstance(par, W.QSplitter) and par.orientation() == qo:
            idx = par.indexOf(dst) + (1 if after else 0)
            sizes = par.sizes()
            par.insertWidget(idx, new)
            n = par.count()
            tot = sum(sizes) or (par.width() if qo == qenum(Qt, 'Orientation.Horizontal') else par.height())
            par.setSizes([max(1, tot // n)] * n)
        else:
            sp = W.QSplitter(qo)
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(5)
            self._replace(dst, sp)
            if after:
                sp.addWidget(dst)
                sp.addWidget(new)
            else:
                sp.addWidget(new)
                sp.addWidget(dst)
            tot = dst.width() if qo == qenum(Qt, 'Orientation.Horizontal') else dst.height()
            sp.setSizes([max(1, tot // 2)] * 2)
        if sess is not None:
            sess.setParent(None)
            new.set_session(sess)
        new.show()
        self.active = None
        self.set_active(new)
        self.main.workspace_changed()

    def close_pane(self, pane, ask=True):
        s = pane.session
        if s is not None:
            if ask and not self.main.confirm_close(s):
                return
            pane.set_session(None)
            self.main.destroy_session(s)
        if self.zoomed is pane:
            self.toggle_zoom(pane)
        par = pane.parentWidget()
        if not isinstance(par, W.QSplitter):
            pane.refresh_title()        # son bolme: bos kalir
            return
        pane.setParent(None)
        pane.deleteLater()
        if par.count() == 1:
            child = par.widget(0)
            self._replace(par, child)
            par.deleteLater()
        rest = self.panes()
        if self.active is pane or self.active not in rest:
            self.active = None
            self.set_active(rest[0])

    def toggle_zoom(self, pane):
        if self.zoomed is None:
            self.zoomed = pane
            self._zoom_vis(self.root, pane)
            pane.btns['zoom'].setIcon(_icon('unzoom'))
        else:
            z, self.zoomed = self.zoomed, None
            self._zoom_vis(self.root, None)
            try:
                z.btns['zoom'].setIcon(_icon('zoom'))
            except RuntimeError:
                pass
        self.set_active(pane)

    def _zoom_vis(self, w, target):
        """target disindaki her seyi gizle (target None: hepsini goster). True: alt agacta target var."""
        if isinstance(w, Pane):
            w.setVisible(target is None or w is target)
            return w is target
        if isinstance(w, W.QSplitter):
            has_any = False
            for i in range(w.count()):
                has_any = self._zoom_vis(w.widget(i), target) or has_any
            w.setVisible(target is None or has_any)
            return has_any
        return False

    def show_session(self, s, split=False):
        """Oturumu goster: gorunuyorsa o bolmeye gec; degilse aktif bolmeye koy (veya yanina ac)."""
        p = self.pane_of(s)
        if p is not None:
            if self.zoomed is not None and self.zoomed is not p:
                self.toggle_zoom(self.zoomed)
            self.set_active(p)
            return p
        if s.parentWidget() is not self.holder:
            s.setParent(self.holder)
        tgt = self.active or self.panes()[0]
        if split and tgt.session is not None:
            s.setParent(None)
            return self.split_pane(tgt, None, s)
        tgt.set_session(s)
        self.set_active(tgt)
        self.main.workspace_changed()
        return tgt

    def focus_neighbor(self, step):
        ps = self.panes()
        if not ps:
            return
        i = ps.index(self.active) if self.active in ps else 0
        self.set_active(ps[(i + step) % len(ps)])

    # -- kaydet / yukle
    def dump(self, w=None):
        w = self.root if w is None else w
        if isinstance(w, Pane):
            return {'kind': w.session.KIND if w.session else ''}
        return {'o': 'h' if w.orientation() == qenum(Qt, 'Orientation.Horizontal') else 'v',
                'sizes': w.sizes(), 'c': [self.dump(w.widget(i)) for i in range(w.count())]}

    def load(self, tree, make):
        """tree'den bolmeleri kur; make(kind) -> oturum."""
        def build(t):
            if 'kind' in t:
                return Pane(self, make(t['kind']) if t['kind'] else None)
            sp = W.QSplitter(qenum(Qt, 'Orientation.Horizontal' if t.get('o') == 'h' else 'Orientation.Vertical'))
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(5)
            for c in t.get('c', []):
                sp.addWidget(build(c))
            if t.get('sizes'):
                sp.setSizes([int(x) for x in t['sizes']])
            return sp
        new = build(tree)
        old = self.root
        self._replace(old, new)
        old.deleteLater()
        self.active = None
        self.set_active(self.panes()[0])


class Sidebar(W.QWidget):
    """Sol panel: araclar (adina tikla = aktif bolmede ac, + = yanina yeni bolme) ve acik pencereler."""

    GROUPS = (('BAGLANTI', None), ('AG ARACLARI', None))

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.setMinimumWidth(170)
        lay = W.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 2, 6)
        lay.setSpacing(2)
        self.setObjectName('sidebar')
        self.setAttribute(qenum(Qt, 'WidgetAttribute.WA_StyledBackground'))
        self.setStyleSheet('QPushButton#tool { text-align: left; padding: 6px 8px; border: none; border-radius: 5px;'
                           ' background: transparent; }'
                           'QPushButton#tool:hover { background: rgba(255,181,71,0.14); }'
                           'QToolButton { border: none; border-radius: 4px; }'
                           'QToolButton:hover { background: rgba(255,181,71,0.20); }'
                           'QLabel#grp { color: #8b929c; font-size: 10px; font-weight: bold; letter-spacing: 1px;'
                           ' padding: 10px 2px 3px 6px; }')
        brand = W.QHBoxLayout()
        ic = W.QLabel()
        pm = QtGui.QPixmap(34, 34)
        pm.fill(QtGui.QColor(0, 0, 0, 0))
        pp = QtGui.QPainter(pm)
        pp.setRenderHint(qenum(QtGui.QPainter, 'RenderHint.Antialiasing'))
        draw_bys_icon(pp, 34)
        pp.end()
        ic.setPixmap(pm)
        nm = W.QLabel(f'<span style="font-size:17px; font-weight:800; color:{BRAND1}">BYS</span>'
                      f'<span style="font-size:17px; font-weight:800; color:{BRAND2}">Term</span>'
                      f'<br><span style="color:#8b929c; font-size:10px">v{APP_VERSION}</span>')
        nm.setObjectName('sidebarBrand')
        brand.addWidget(ic)
        brand.addWidget(nm, 1)
        lay.addLayout(brand)
        for gi, (title, types) in enumerate((('BAGLANTI', SESSION_TYPES), ('AG ARACLARI', NET_TYPES))):
            g = W.QLabel(title)
            g.setObjectName('grp')
            lay.addWidget(g)
            for cls in types:
                row = W.QHBoxLayout()
                row.setSpacing(0)
                b = W.QPushButton(cls.TITLE)
                b.setObjectName('tool')
                b.setIcon(tool_icon(cls.KIND))
                b.setIconSize(QtCore.QSize(18, 18))
                b.setToolTip(tx('{t}: seçili pencerenin içeriğini buna çevir').replace('{t}', tx(cls.TITLE)))
                b.clicked.connect(lambda _=False, c=cls: main.open_tool(c, split=False))
                plus = W.QToolButton()
                plus.setIcon(_icon('plus', ACC))
                plus.setAutoRaise(True)
                plus.setToolTip(tx('Yeni {t}: aktif bolmenin YANINA ac (Terminator gibi)').replace('{t}', tx(cls.TITLE)))
                plus.clicked.connect(lambda _=False, c=cls: main.open_tool(c, split=True))
                row.addWidget(b, 1)
                row.addWidget(plus)
                lay.addLayout(row)
        lay.addStretch(1)
        hint = W.QLabel('Araç adı: seçili pencerede aç\n+ : yanına yeni pencere ekle\n\n'
                        'Ctrl+Shift+E yana böl · O alta böl\nX tam ekran · W kapat · Ctrl+Tab geç')
        hint.setStyleSheet('color:#777; font-size: 10px;')
        lay.addWidget(hint)

    def refresh(self, sessions, ws):
        pass        # acik pencereler listesi kaldirildi; paneller zaten sagda gorunuyor


class MainWindow(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'{APP_NAME} {APP_VERSION}')
        self.resize(1400, 860)
        self.settings = QtCore.QSettings(APP_NAME, APP_NAME)
        self.last_dir = str(self.settings.value('last_dir', os.path.expanduser('~')))
        self.ports = core.list_serial_ports()
        self._port_keys = [p.key() for p in self.ports]
        self.ifaces = []
        self.sessions = []

        tb = self.addToolBar('Ana')
        tb.setMovable(False)
        setb = W.QToolButton()
        setb.setText('  ' + tr('Settings') + '  ')
        setb.setPopupMode(qenum(W.QToolButton, 'ToolButtonPopupMode.InstantPopup'))
        sm = W.QMenu(setb)
        self.act_admin = sm.addAction('Acilista yonetici izni iste (IP degistirme, seri port izni)')
        self.act_admin.setCheckable(True)
        self.act_admin.setChecked(str(self.settings.value('ask_admin', 'true')).lower() in ('1', 'true'))
        self.act_admin.toggled.connect(lambda v: self.settings.setValue('ask_admin', v))
        sm.addAction('Yonetici iznini simdi iste').triggered.connect(self.request_admin)
        sm.addSeparator()
        lm = sm.addMenu(tr('Language') + ' / Dil')
        cur = self.settings.value('lang', 'tr')
        for code, label in LANGS:
            a = lm.addAction(label)
            a.setCheckable(True)
            a.setChecked(code == cur)
            a.triggered.connect(lambda _=False, c=code: self.set_language(c))
        tm = sm.addMenu(tr('Theme'))
        dark = str(self.settings.value('theme', 'dark')) != 'light'
        for code, label in (('dark', tr('Dark')), ('light', tr('Light'))):
            a = tm.addAction(label)
            a.setCheckable(True)
            a.setChecked((code == 'dark') == dark)
            a.triggered.connect(lambda _=False, c=code: self.set_theme(c))
        for key, label, default in (('splash', tr('Show startup animation'), 'true'),
                                    ('check_updates', tr('Check for updates at startup'), 'true')):
            a = sm.addAction(label)
            a.setCheckable(True)
            a.setChecked(str(self.settings.value(key, default)).lower() in ('1', 'true'))
            a.toggled.connect(lambda v, k=key: self.settings.setValue(k, v))
        sm.addAction(tr('Check for updates now')).triggered.connect(lambda: self.check_updates(manual=True))
        sm.addSeparator()
        sm.addAction('Pencere duzenini sifirla').triggered.connect(self.reset_layout)
        setb.setMenu(sm)
        tb.addWidget(setb)
        tb.addAction(tr('About')).triggered.connect(self.about)

        # sol panel (araclar) | sag: Terminator tarzi bolmeler
        self.ws = Workspace(self)
        self.sidebar = Sidebar(self)
        split = W.QSplitter(qenum(Qt, 'Orientation.Horizontal'))
        split.addWidget(self.sidebar)
        split.addWidget(self.ws)
        split.setStretchFactor(1, 1)
        split.setSizes([210, 1190])
        split.setChildrenCollapsible(False)
        self.setCentralWidget(split)
        self._shortcuts()

        self.statusBar().showMessage(f'{len(self.ports)} seri port bulundu   |   Qt: {QT_API}', 5000)
        self.lbl_admin = W.QLabel()
        self.statusBar().addPermanentWidget(self.lbl_admin)
        self.lbl_ports = W.QLabel()
        self.statusBar().addPermanentWidget(self.lbl_ports)
        self._update_port_label()
        self._restore_layout()

        # port tarama arka planda (Windows'ta comports() onlarca ms surebilir)
        self._scan_result = None
        self._scan_lock = threading.Lock()
        self._scan_now = threading.Event()
        self._scan_stop = threading.Event()
        threading.Thread(target=self._scan_loop, daemon=True, name='port-scan').start()
        self.scan_timer = QtCore.QTimer(self)
        self.scan_timer.timeout.connect(self._apply_scan)
        self.scan_timer.start(300)

        geo = self.settings.value('geometry')
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass

    # -- kisayollar (Terminator ile ayni)
    def _shortcuts(self):
        Act = getattr(QtGui, 'QAction', None) or getattr(W, 'QAction')
        for keys, fn in (('Ctrl+Shift+E', lambda: self.ws.split_pane(self.ws.active, 'h')),
                         ('Ctrl+Shift+O', lambda: self.ws.split_pane(self.ws.active, 'v')),
                         ('Ctrl+Shift+X', lambda: self.ws.toggle_zoom(self.ws.active)),
                         ('Ctrl+Shift+W', lambda: self.ws.close_pane(self.ws.active)),
                         ('Ctrl+Tab', lambda: self.ws.focus_neighbor(1)),
                         ('Ctrl+Shift+Tab', lambda: self.ws.focus_neighbor(-1))):
            a = Act(self)
            a.setShortcut(QtGui.QKeySequence(keys))
            a.setShortcutContext(qenum(Qt, 'ShortcutContext.ApplicationShortcut'))
            a.triggered.connect(fn)
            self.addAction(a)

    # -- duzen
    def _restore_layout(self):
        raw = self.settings.value('layout', '')
        try:
            tree = json.loads(raw) if raw else None
        except ValueError:
            tree = None
        kinds = {c.KIND: c for c in ALL_TYPES}
        if tree:
            try:
                self.ws.load(tree, lambda k: self.create_session(kinds[k]) if k in kinds else None)
                return
            except Exception as e:     # noqa: BLE001
                sys.stderr.write(f'duzen yuklenemedi: {e}\n')
        self.ws.show_session(self.create_session(SerialSession))

    def reset_layout(self):
        for p in list(self.ws.panes())[1:]:
            self.ws.close_pane(p)
        self.workspace_changed()

    def workspace_changed(self):
        ws = getattr(self, 'ws', None)
        if ws is None:
            return
        if hasattr(self, 'sidebar'):
            self.sidebar.refresh(self.sessions, ws)
        if getattr(ws, 'active', None) is not None and ws.active.session is not None:
            self.setWindowTitle(f'{APP_NAME} {APP_VERSION} — {tx(self.ws.active.session.tab_label())}')

    # -- portlar
    def _scan_loop(self):
        while not self._scan_stop.is_set():
            ports = core.list_serial_ports()
            with self._scan_lock:
                self._scan_result = ports
            self._scan_now.wait(1.5)
            self._scan_now.clear()

    def rescan_ports_now(self):
        self._scan_now.set()

    def _apply_scan(self):
        with self._scan_lock:
            ports, self._scan_result = self._scan_result, None
        if ports is None:
            return
        keys = [p.key() for p in ports]
        if keys == self._port_keys:
            return
        old = {p.device for p in self.ports}
        new = {p.device for p in ports}
        added, removed = new - old, old - new
        self.ports, self._port_keys = ports, keys
        msg = []
        if added:
            msg.append('Yeni port: ' + ', '.join(p.label for p in ports if p.device in added))
        if removed:
            msg.append('Cikarilan port: ' + ', '.join(sorted(removed)))
        if msg:
            self.statusBar().showMessage('   |   '.join(msg), 8000)
        for w in self.sessions:
            w.update_ports(ports)
        self._update_port_label()

    def _update_port_label(self):
        self.lbl_ports.setText(f'Seri portlar: {len(self.ports)}  ')
        self.lbl_ports.setToolTip('\n'.join(p.label for p in self.ports) or 'Port yok')

    # -- oturumlar
    def create_session(self, cls):
        s = cls(self)
        s.setParent(self.ws.holder)
        self.sessions.append(s)
        self.workspace_changed()
        return s

    def open_tool(self, cls, split=False):
        """Araç adına tıkla (split=False): AKTİF (en son seçilen) pencerenin içeriği o araçla
        değişir — yeni pencere açılmaz. + (split=True): aktif pencerenin yanına yeni pencere ekler."""
        if not split:
            tgt = self.ws.active or self.ws.panes()[0]
            old = tgt.session
            if old is not None and type(old) is cls:
                self.ws.set_active(tgt)
                return old
            if old is not None and not self.confirm_close(old):
                return old
            s = self.create_session(cls)
            tgt.set_session(s)                 # eskisi holder'a gider -> kapat
            if old is not None:
                self.destroy_session(old)
            self.ws.set_active(tgt)
            self.workspace_changed()
            return s
        s = self.create_session(cls)
        tgt = self.ws.active or self.ws.panes()[0]
        if tgt.session is None:
            tgt.set_session(s)
            self.ws.set_active(tgt)
        else:
            self.ws.split_pane(tgt, None, s)
        self.workspace_changed()
        return s

    def add_session(self, cls, focus=True):
        return self.open_tool(cls, split=True)

    def confirm_close(self, s):
        if s.transport is not None or getattr(s, 'is_busy', lambda: False)():
            r = W.QMessageBox.question(self, 'Kapat', f'{s.tab_label()}: baglanti / test calisiyor. Kapatilsin mi?')
            return r == qenum(W.QMessageBox, 'StandardButton.Yes')
        return True

    def destroy_session(self, s):
        try:
            s.store_settings()
        except Exception:
            pass
        s.shutdown()
        if s in self.sessions:
            self.sessions.remove(s)
        s.setParent(None)
        s.deleteLater()
        self.workspace_changed()

    def close_session(self, s):
        p = self.ws.pane_of(s)
        if p is not None:
            self.ws.close_pane(p)
        elif self.confirm_close(s):
            self.destroy_session(s)

    def session_state_changed(self, s):
        ws = getattr(self, 'ws', None)
        if ws is None:
            return
        p = ws.pane_of(s)
        if p is not None:
            p.refresh_title()
        self.workspace_changed()

    # -- ag araclari arasi baglantilar
    def ifaces_changed(self, ifaces):
        self.ifaces = ifaces
        for w in self.sessions:
            if isinstance(w, ScanTab):
                w.ifaces_changed(ifaces)

    def _first(self, cls):
        for s in self.sessions:
            if isinstance(s, cls):
                if self.ws.pane_of(s) is None:
                    self.ws.show_session(s, split=True)
                else:
                    self.ws.show_session(s)
                return s
        return self.open_tool(cls, split=True)

    def send_to_ping(self, ip):
        self._first(PingTab).add_host(ip)

    def send_to_iperf(self, ip):
        t = self._first(IperfTab)
        t.rb_client.setChecked(True)
        t.ed_host.setText(ip)

    def send_to_tcp(self, ip):
        self._first(TcpClientSession).host.setText(ip)

    # -- yonetici izni
    def admin_state(self):
        if net.is_admin():
            return True, '<span style="color:#3DDC84">🔓 Yonetici: aktif</span>'
        h = net.PrivHelper.instance
        if h is not None and h.alive:
            return True, '<span style="color:#3DDC84">🔓 Yonetici izni: verildi</span>'
        return False, '<span style="color:#888">🔒 Yonetici izni yok (IP degisikliginde sorulur)</span>'

    def _admin_changed(self):
        self.lbl_admin.setText(self.admin_state()[1] + '  ')
        for w in self.sessions:
            if isinstance(w, NetConfigTab):
                w.update_admin_label()

    def request_admin(self, *_):
        if net.is_admin():
            self._admin_changed()
            return
        if net.IS_WIN:
            r = W.QMessageBox.question(self, 'Yonetici izni', 'BYSTerm yonetici olarak yeniden baslatilsin mi?\n'
                                       '(Acik baglantilar kapanir.)')
            if r == qenum(W.QMessageBox, 'StandardButton.Yes') and net.relaunch_as_admin():
                self.close()
            return
        h = net.PrivHelper.get()
        if h.alive:
            self._admin_changed()
            return
        self.lbl_admin.setText('🔑 Yonetici izni isteniyor...  ')

        def done(ok, err):
            if not ok:
                self.statusBar().showMessage(f'Yonetici izni verilmedi ({h.err or err}). '
                                             f'IP degistirirken tekrar sorulacak.', 8000)
            self._admin_changed()
        _bg(self, h.start, done)

    def about(self):
        qexec(AboutDialog(self))

    def set_language(self, code):
        self.settings.setValue('lang', code)
        r = W.QMessageBox.question(self, tr('Language'), tr('Restart BYSTerm to apply the new language.') + '\n\n' +
                                   tr('Restart now?'))
        if r == qenum(W.QMessageBox, 'StandardButton.Yes'):
            self.restart()

    def set_theme(self, code):
        self.settings.setValue('theme', code)
        apply_theme(W.QApplication.instance(), code != 'light')
        for w in W.QApplication.instance().allWidgets():
            if isinstance(w, Terminal):
                w.apply_palette()
            elif isinstance(w, LineGraph):
                w.update()
            elif isinstance(w, Pane):
                w.set_active(w is self.ws.active)

    def restart(self):
        cmd = net._self_cmd() + [a for a in sys.argv[1:] if a not in ('--elevated', '--updated')]
        if net.is_admin() and net.IS_WIN:
            cmd.append('--elevated')
        self.close()
        subprocess.Popen(cmd, close_fds=True)
        W.QApplication.instance().quit()

    def check_updates(self, manual=False):
        def done(rel, err):
            if err or not rel:
                if manual:
                    W.QMessageBox.warning(self, APP_NAME, tr('Could not check for updates: {err}').format(err=err))
                return
            newer = upd.parse_version(rel['version']) > upd.parse_version(APP_VERSION)
            if not newer:
                if manual:
                    W.QMessageBox.information(self, APP_NAME, tr('You are using the latest version ({cur}).').format(
                        cur=f'v{APP_VERSION}'))
                return
            if not manual and str(self.settings.value('skip_version', '')) == rel['version']:
                return
            qexec(UpdateDialog(self, rel))
        _bg(self, upd.latest_release, done)

    def closeEvent(self, ev):
        if net.PrivHelper.instance is not None:
            net.PrivHelper.instance.close()
        self._scan_stop.set()
        self._scan_now.set()
        self.settings.setValue('geometry', self.saveGeometry())
        self.settings.setValue('last_dir', self.last_dir)
        try:
            self.settings.setValue('layout', json.dumps(self.ws.dump()))
        except Exception:
            pass
        for w in list(self.sessions):
            try:
                w.store_settings()
            except Exception:
                pass
            w.shutdown()
        super().closeEvent(ev)


def resource(name):
    """assets/ altindaki dosya — hem kaynak koddan hem PyInstaller paketinden."""
    base = getattr(sys, '_MEIPASS', None)
    if base:
        return os.path.join(base, 'assets', name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', name)


def _st(msg):
    if '--selftest' in sys.argv:
        print(f'{APP_NAME} selftest: {msg}', flush=True)


def _safe_stdio():
    # LANG=C olan sistemlerde (ör. Jetson/Ubuntu 18.04 servis/SSH) Python 3.6 konsolu ASCII'dir:
    # Turkce karakter iceren bir print uygulamayi cokertmesin.
    import io
    for name in ('stdout', 'stderr'):
        s = getattr(sys, name, None)
        enc = (getattr(s, 'encoding', None) or '').lower().replace('-', '').replace('_', '')
        if s is None or enc == 'utf8' or not hasattr(s, 'buffer'):
            continue
        try:
            setattr(sys, name, io.TextIOWrapper(s.buffer, encoding=getattr(s, 'encoding', None) or 'ascii',
                                                errors='backslashreplace', line_buffering=True))
        except Exception:
            pass


def main():
    _safe_stdio()
    if '--selftest' in sys.argv:
        # en bastan bekci: baslangicta (Qt/pencere/port tarama) takilsa bile surec biter
        def _early():
            print(f'{APP_NAME} SELFTEST TIMEOUT (baslangic asamasi)', flush=True)
            os._exit(4)
        _t = threading.Timer(90, _early)
        _t.daemon = True
        _t.start()
        _st(f'basladi (Python {sys.version.split()[0]}, {QT_API})')
    if hasattr(Qt, 'AA_EnableHighDpiScaling') and QT_API != 'PySide6':
        QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if core.IS_WIN:
        try:   # gorev cubugunda python ikonu yerine uygulama ikonu
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f'{APP_NAME}.{APP_VERSION}')
        except Exception:
            pass
    if sys.platform.startswith('linux'):
        # Arayuz OpenGL kullanmaz; GL entegrasyonunu kapatmak suruculu/surucusuz (Jetson,
        # sanal makine, uzak masaustu) her sistemde sorunsuz acilmayi saglar.
        os.environ.setdefault('QT_XCB_GL_INTEGRATION', 'none')
    app = W.QApplication(sys.argv)
    _st('QApplication hazir')
    selftest = '--selftest' in sys.argv
    cfg = QtCore.QSettings(APP_NAME, APP_NAME)
    on = lambda k, d='true': str(cfg.value(k, d)).lower() in ('1', 'true')   # noqa: E731
    set_lang(str(cfg.value('lang', 'tr')))
    install_i18n()
    ask_admin = on('ask_admin')
    if (core.IS_WIN and ask_admin and not selftest and not net.is_admin()
            and '--elevated' not in sys.argv):
        # acilista BIR KEZ UAC: onaylanirsa yonetici olarak yeniden baslar, reddedilirse normal devam
        if net.relaunch_as_admin():
            sys.exit(0)
    app.setApplicationName(APP_NAME)
    if hasattr(app, 'setDesktopFileName'):
        app.setDesktopFileName('bysterm')
    apply_theme(app, str(cfg.value('theme', 'dark')) != 'light')
    icon = resource('icon.png')
    if os.path.exists(icon):
        app.setWindowIcon(QtGui.QIcon(icon))
    upd.cleanup_old()
    upd.linux_desktop_integration(icon)      # Linux: uygulama menusune simgesiyle ekle
    win = MainWindow()
    _st('ana pencere olustu')

    def show_main():
        win.setWindowOpacity(0.0)
        win.show()
        win._admin_changed()
        fade = QtCore.QVariantAnimation(win) if hasattr(QtCore, 'QVariantAnimation') else None
        if fade is not None:
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setDuration(320)
            fade.valueChanged.connect(lambda v: win.setWindowOpacity(float(v)))
            fade.start()
            win._fade = fade
        else:
            win.setWindowOpacity(1.0)
        if selftest:
            win.setWindowOpacity(1.0)
            QtCore.QTimer.singleShot(300, lambda: _selftest(win))
            return
        if ask_admin and not core.IS_WIN and not net.is_admin():
            QtCore.QTimer.singleShot(400, win.request_admin)
        if on('check_updates') and upd.is_frozen():
            QtCore.QTimer.singleShot(2500, lambda: win.check_updates(manual=False))
    if on('splash') and not selftest:
        sp = Splash(show_main)
        sp.show()
        win._splash = sp
    else:
        show_main()
    sys.exit(qexec(app))


def _selftest(win):
    """Paketlenmis uygulamanin bu sistemde calistigini dogrular (CI ve kullanici icin):
    pencere acilir, kendi icinde TCP sunucu <-> istemci veri alisverisi yapilir."""
    step = {'n': 0}
    names = ('sunucu aciliyor', 'istemci baglaniyor', 'veri bekleniyor')

    def watchdog():   # olay dongusu kilitlense bile surec mutlaka biter
        print(f'{APP_NAME} SELFTEST TIMEOUT (adim: {names[min(step["n"], 2)]})', flush=True)
        os._exit(3)
    wd = threading.Timer(60, watchdog)
    wd.daemon = True
    wd.start()
    import bysterm_i18n
    print(f'{APP_NAME} selftest: pencere acildi (dil={bysterm_i18n.LANG}, '
          f'{len(bysterm_i18n.FULL)} ceviri, ornek: {tx("Baglan")!r})', flush=True)
    srv = win.add_session(TcpServerSession)
    cli = win.add_session(TcpClientSession)
    srv.port.setValue(0)
    srv.bind.setCurrentText('127.0.0.1')
    srv.open_conn()
    t0 = time.monotonic()

    def poll():
        ok = False
        if srv.connected and cli.transport is None and step['n'] == 0:
            cli.host.setText('127.0.0.1')
            cli.port.setValue(srv.transport.port)
            cli.open_conn()
            step['n'] = 1
        if cli.connected and step['n'] == 1:
            cli.send_edit.setEditText('selftest\\x00\\xff')
            cli.send_now()
            step['n'] = 2
        if step['n'] == 2 and srv.rx_total >= 10:
            ok = True
        if ok or time.monotonic() - t0 > 10:
            if not ok:
                return finish(False, 'TCP alisverisi basarisiz')
            print(f'{APP_NAME} selftest: TCP OK, ag testleri...', flush=True)
            _bg(win, _net_selftest, lambda r, e: finish(*(r if r else (False, f'ag testi hatasi: {e}'))))
            return
        QtCore.QTimer.singleShot(50, poll)
    def finish(ok, detail):
        wd.cancel()
        ports = len(win.ports)
        print(f'{APP_NAME} {APP_VERSION} SELFTEST {"OK" if ok else "FAIL"} '
              f'(Qt {QtCore.qVersion()} / {QT_API}, Python {sys.version.split()[0]}, '
              f'{sys.platform}, seri port: {ports}) {detail}', flush=True)
        win.close()
        W.QApplication.instance().exit(0 if ok else 1)
        threading.Timer(5, lambda: os._exit(0 if ok else 1)).start()   # kapanis takilirsa
    poll()


def _net_selftest():
    """Ag katmani bu sistemde calisiyor mu: arayuz listesi, ping, iperf3 dongu testi."""
    lst = net.list_interfaces(include_virtual=True)
    p = net.Pinger()
    r = p.ping('127.0.0.1', 2000)
    backend = p.backend
    p.close()
    srv = net.IperfServer(0, bind='127.0.0.1', once=True)
    srv.start()
    c = net.IperfClient('127.0.0.1', srv.port, duration=1)
    c.start()
    t0 = time.monotonic()
    while c.running and time.monotonic() - t0 < 15:
        time.sleep(0.05)
    res = [e[2] for e in list(c.events) if e[0] == 'result']
    srv.stop()
    rate = res[0]['recv_bps'] if res else 0
    detail = (f'| ag: {len(lst)} arayuz, ping={"OK" if r.ok else "YOK"}({backend}'
              f'{", %.2f ms" % r.rtt if r.ok else ""}), iperf={net.fmt_rate(rate)}')
    ok = len(lst) > 0 and r.ok and rate > 0
    if not lst:
        detail += ' [arayuz listesi BOS]'
    return ok, detail


if __name__ == '__main__':
    main()
