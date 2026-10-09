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
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if '--net-helper' in sys.argv:          # yonetici yardimcisi (root): Qt/pyserial YUKLENMEZ
    import bysterm_net
    bysterm_net.helper_main(sys.argv[sys.argv.index('--net-helper') + 1:])
    sys.exit(0)

try:
    import serial  # noqa: F401
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
from bysterm_core import RX, TX  # noqa: E402

APP_NAME = 'BYSTerm'
APP_VERSION = '1.1.0'

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

COLORS = {
    'bg': '#1b1d23', 'fg': '#d6d6d6',
    RX: '#7ee787', TX: '#79c0ff', 'hdr': '#7d8590',
    'info': '#e3b341', 'warn': '#f0883e', 'error': '#ff7b72',
}


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
        self.setStyleSheet(f'QPlainTextEdit {{ background:{COLORS["bg"]}; color:{COLORS["fg"]};'
                           f' border:1px solid #30363d; }}')
        self.formats = {}
        for k, c in COLORS.items():
            if k in ('bg', 'fg'):
                continue
            tf = QtGui.QTextCharFormat()
            tf.setForeground(QtGui.QColor(c))
            self.formats[k] = tf
        self._plain = QtGui.QTextCharFormat()
        self._plain.setForeground(QtGui.QColor(COLORS['fg']))

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


# =========================================================================== seri ayar paneli
class SerialSettings(W.QWidget):
    """Port + baud + format secimi (Seri ve Seri izleme sekmelerinde ortak)."""

    def __init__(self, main, with_port=True, parent=None):
        super().__init__(parent)
        self.main = main
        lay = W.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.port = W.QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(300)
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
        self.parity.addItems(list(core.PARITIES))
        self.stop = W.QComboBox()
        self.stop.addItems(list(core.STOPBITS))
        self.flow = W.QComboBox()
        self.flow.addItems(core.FLOWS)

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
                                 core.PARITIES[self.parity.currentText()],
                                 core.STOPBITS[self.stop.currentText()],
                                 self.flow.currentText())

    def save(self, st, prefix):
        st.setValue(f'{prefix}/port', self.current_device())
        for k in ('baud', 'bits', 'parity', 'stop', 'flow'):
            st.setValue(f'{prefix}/{k}', getattr(self, k).currentText())

    def load(self, st, prefix):
        dev = st.value(f'{prefix}/port', '')
        if dev:
            idx = self.port.findData(dev)
            if idx >= 0:
                self.port.setCurrentIndex(idx)
        for k in ('baud', 'bits', 'parity', 'stop', 'flow'):
            v = st.value(f'{prefix}/{k}', None)
            if v:
                cb = getattr(self, k)
                if cb.isEditable():
                    cb.setCurrentText(str(v))
                else:
                    i = cb.findText(str(v))
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
        row = W.QHBoxLayout()
        self.build_connection(row, cl)
        self.btn_connect = W.QPushButton(self.connect_text())
        self.btn_connect.setMinimumWidth(110)
        self.btn_connect.setDefault(False)
        self.btn_connect.clicked.connect(self.toggle_connection)
        row.addWidget(self.btn_connect)
        cl.insertLayout(0, row)
        root.addWidget(self.conn_box)

        # --- gorunum secenekleri
        opt = W.QHBoxLayout()
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
        sl = W.QHBoxLayout(self.send_box)
        sl.setContentsMargins(0, 0, 0, 0)
        self.send_edit = W.QComboBox()
        self.send_edit.setEditable(True)
        self.send_edit.setInsertPolicy(qenum(W.QComboBox, 'InsertPolicy.NoInsert'))
        self.send_edit.setMaxCount(50)
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

        self.fmt = core.Formatter(labels=self.labels())
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
            for key, w in (('send_mode', self.send_mode), ('eol', self.eol)):
                v = st.value(f'{p}/{key}', None)
                if v is not None:
                    i = w.findText(str(v))
                    if i >= 0:
                        w.setCurrentIndex(i)
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
        st.setValue(f'{p}/eol', self.eol.currentText())
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
        t.close()
        self._drain_transport(t)       # kapanirken gelen son veriler
        self.info(reason or 'Baglanti kapatildi')
        self.set_connected(False)

    def set_busy(self):
        self.btn_connect.setText('Iptal')
        self.lbl_state.setText('Aciliyor...')
        self.set_inputs_enabled(False)

    def set_connected(self, on):
        self.connected = on
        self.btn_connect.setText(self.disconnect_text() if on else self.connect_text())
        if on:
            self.lbl_state.setText(f'<b style="color:#2da44e">● ACIK</b>  {self.transport.description}')
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
            f'<span style="color:#2da44e"><b>{lb[RX].rstrip(">")}</b></span> {hb(self.rx_total)} '
            f'({hb(self.rx_rate)}/s) &nbsp;&nbsp; '
            f'<span style="color:#1f6feb"><b>{lb[TX].rstrip(">")}</b></span> {hb(self.tx_total)} '
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
        data += {'CR': b'\r', 'LF': b'\n', 'CR+LF': b'\r\n'}.get(self.eol.currentText(), b'')
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
        ctl = W.QHBoxLayout()
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
            bg = '#2da44e' if on else ('#444' if lines is not None else '#2a2a2a')
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
        box.setMinimumWidth(200)
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
        self.ss = SerialSettings(self.main)
        self.ss.port.lineEdit().setPlaceholderText('Cihazin bagli oldugu GERCEK port')
        row.addWidget(W.QLabel('Gercek'))
        row.addWidget(self.ss, 1)

        r2 = W.QHBoxLayout()
        self.virt = W.QComboBox()
        self.virt.setEditable(True)
        self.virt.setMinimumWidth(260)
        self.chk_follow = W.QCheckBox('Uygulamanin baud/format ayarini takip et')
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip('Diger uygulama sanal portu hangi baud ile acarsa gercek port '
                                   'de o baud\'a gecer (Linux/macOS)')
        self.chk_passive = W.QCheckBox('Pasif dinleme (2 gercek port, iletim yok)')
        self.chk_passive.setToolTip(
            'Donanim "tap" modu: iki USB-seri ceviricinin RX ucu izlenen hattin TX ve RX\'ine\n'
            'baglanir; BYSTerm ikisini de sadece dinler ve tek zaman cizelgesinde gosterir.')
        self.chk_passive.toggled.connect(self._passive_toggled)
        self.lbl_v = W.QLabel('Sanal port')
        r2.addWidget(self.lbl_v)
        r2.addWidget(self.virt, 1)
        r2.addWidget(self.chk_follow)
        r2.addWidget(self.chk_passive)
        col.addLayout(r2)

        self.help = W.QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet('color:#888')
        col.addWidget(self.help)
        self.chk_follow.setVisible(core.IS_POSIX)
        self._fill_virt(self.main.ports)
        self._passive_toggled(False)

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
        if self.chk_passive.isChecked():
            return {RX: 'A>', TX: 'B>'}
        return {RX: 'CIHAZ>', TX: 'UYGUL>'}

    def connect_text(self):
        return 'Izlemeyi baslat'

    def disconnect_text(self):
        return 'Durdur'

    def make_transport(self):
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
        for w in (self.ss, self.virt, self.chk_follow, self.chk_passive):
            w.setEnabled(en)
        if en:
            self.chk_follow.setEnabled(not self.chk_passive.isChecked())

    def tab_label(self):
        return 'Izleme ' + os.path.basename(self.ss.current_device() or '')

    def update_ports(self, ports):
        if not self.transport:
            self.ss.update_ports(ports)
            self._fill_virt(ports)

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

    def __init__(self, unit='', color='#79c0ff', maxpts=120, fmt='{:.1f}', parent=None):
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
        p.fillRect(r, QtGui.QColor(COLORS['bg']))
        L, T, R, B = 52, 18, r.width() - 8, r.height() - 8
        vals = list(self.vals)
        good = [v for v in vals if v is not None]
        top = max(good) * 1.2 if good else 1.0
        top = top or 1.0
        p.setPen(QtGui.QPen(QtGui.QColor('#30363d'), 1))
        f = p.font()
        f.setPointSize(max(7, f.pointSize() - 2))
        p.setFont(f)
        for i in range(5):
            y = T + (B - T) * i / 4.0
            p.setPen(QtGui.QPen(QtGui.QColor('#30363d'), 1))
            p.drawLine(QtCore.QPointF(L, y), QtCore.QPointF(R, y))
            p.setPen(QtGui.QColor(COLORS['hdr']))
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
                p.setPen(QtGui.QPen(QtGui.QColor(COLORS['error']), 2))
                p.drawLine(QtCore.QPointF(x, B), QtCore.QPointF(x, B - 10))
                continue
            pts.append(QtCore.QPointF(x, B - (B - T) * v / top))
        if len(pts) > 1:
            p.setPen(QtGui.QPen(QtGui.QColor(self.color), 2))
            p.drawPolyline(QtGui.QPolygonF(pts))
        elif len(pts) == 1:
            p.setBrush(QtGui.QColor(self.color))
            p.drawEllipse(pts[0], 2, 2)
        p.setPen(QtGui.QColor(COLORS['fg']))
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
    return '' if t.lower() == 'otomatik' else t


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
        self.btn_apply.setStyleSheet('QPushButton { font-weight: bold; padding: 4px 18px; }')
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
            col = '#2da44e' if it.up else '#888'
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
        top = W.QHBoxLayout()
        self.ed_hosts = W.QLineEdit(main.settings.value('ping/hosts', '8.8.8.8') or '8.8.8.8')
        self.ed_hosts.setPlaceholderText('Hedef(ler): 192.168.1.10, 192.168.1.20, google.com')
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
        self.graph = LineGraph('ms', '#7ee787', fmt='{:.1f}')
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
        vals = {1: w.ip or '?', 2: ('● cevap veriyor' if r.ok else '✖ ' + r.err),
                3: f(r.rtt if r.ok else None), 4: f(avg), 5: f(st['min']), 6: f(st['max']),
                7: f(st['jit'] if st['recv'] > 1 else None), 8: f'%{loss:.1f}',
                9: f'{st["sent"]} / {st["recv"]}'}
        for c, v in vals.items():
            it = self.table.item(row, c)
            if it:
                it.setText(v)
                if c == 2:
                    it.setForeground(QtGui.QBrush(QtGui.QColor('#2da44e' if r.ok else COLORS['error'])))
        if changed or first:
            if changed or not r.ok:
                txt = f'{h}: ▲ CEVAP VERMEYE BASLADI' if r.ok else f'{h}: ▼ CEVAP KESILDI ({r.err})'
                if first and not r.ok:
                    txt = f'{h}: cevap vermiyor ({r.err})'
                segs += self.fmt.text_line(ts, 'info' if r.ok else 'error', txt)
                if self.chk_beep.isChecked() and changed:
                    W.QApplication.beep()
        if not self.chk_changes.isChecked():
            stamp = time.strftime('%H:%M:%S', time.localtime(ts)) + f'.{int((ts % 1) * 1000):03d}'
            if r.ok:
                ttl = f'  TTL={r.ttl}' if r.ttl else ''
                segs.append(('hdr', f'{stamp} '))
                segs.append((RX, f'{h} ({w.ip})  sira={seq}  sure={r.rtt:.2f} ms{ttl}\n'))
            else:
                segs.append(('hdr', f'{stamp} '))
                segs.append(('error', f'{h}  sira={seq}  {r.err}\n'))
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
        top = W.QHBoxLayout()
        self.cb_if = W.QComboBox()
        self.cb_if.setMinimumWidth(260)
        self.cb_if.currentIndexChanged.connect(self._if_changed)
        self.ed_range = W.QLineEdit('192.168.1.1-254')
        self.ed_range.setPlaceholderText('192.168.1.0/24  veya  192.168.1.1-254  veya  10.0.0.5-10.0.0.40')
        self.ed_range.returnPressed.connect(self.toggle)
        self.sp_to = W.QSpinBox()
        self.sp_to.setRange(100, 5000)
        self.sp_to.setValue(500)
        self.sp_to.setSuffix(' ms')
        self.btn = W.QPushButton('Tara')
        self.btn.setMinimumWidth(100)
        self.btn.clicked.connect(self.toggle)
        top.addWidget(W.QLabel('Ag karti'))
        top.addWidget(self.cb_if)
        top.addWidget(W.QLabel('Aralik'))
        top.addWidget(self.ed_range, 1)
        top.addWidget(W.QLabel('Zaman asimi'))
        top.addWidget(self.sp_to)
        top.addWidget(self.btn)
        root.addLayout(top)
        self.prog = W.QProgressBar()
        self.prog.setTextVisible(True)
        root.addWidget(self.prog)
        self.table = W.QTableWidget()
        _setup_table(self.table, ['IP', 'Sure', 'TTL (tahmini sistem)', 'MAC', 'Host adi'], 4)
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
                guess = '' if ttl is None else (' Linux/Jetson/cihaz' if ttl <= 64 else
                                                (' Windows' if ttl <= 128 else ' ag cihazi'))
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
        r1 = W.QHBoxLayout()
        self.rb_client = W.QRadioButton('Istemci (karsiya baglan)')
        self.rb_server = W.QRadioButton('Sunucu (bekle)')
        self.rb_client.setChecked(True)
        self.rb_client.toggled.connect(self._mode)
        self.ed_host = W.QLineEdit(st.value('iperf/host', '') or '')
        self.ed_host.setPlaceholderText('karsi cihazin IP\'si (orada: iperf3 -s)')
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
        r2 = W.QHBoxLayout()
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
        self.lbl_rate.setStyleSheet('color:#79c0ff')
        self.lbl_sum = W.QLabel('')
        self.lbl_sum.setStyleSheet('color:#888')
        big.addWidget(self.lbl_rate)
        big.addSpacing(20)
        big.addWidget(self.lbl_sum, 1)
        root.addLayout(big)
        self.graph = LineGraph('Mbit/s', '#79c0ff', fmt='{:.0f}')
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
                lines = [f'── SONUC ({r["elapsed"]:.1f} sn) ──',
                         f'  Gonderen: {net.fmt_bytes(r["sent_bytes"]):>10}  {net.fmt_rate(r["sent_bps"]):>14}',
                         f'  Alan    : {net.fmt_bytes(r["recv_bytes"]):>10}  {net.fmt_rate(r["recv_bps"]):>14}']
                summ = f'Alan tarafta olculen: {net.fmt_rate(r["recv_bps"])}'
                if r.get('udp'):
                    pk = r.get('packets') or 0
                    lost = r.get('lost', 0)
                    pct = 100.0 * lost / pk if pk else 0
                    lines.append(f'  UDP     : jitter {r.get("jitter_ms", 0):.3f} ms   kayip {lost}/{pk} (%{pct:.2f})')
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
class MainWindow(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'{APP_NAME} {APP_VERSION} — Seri / TCP / UDP test')
        self.resize(1200, 760)
        self.settings = QtCore.QSettings(APP_NAME, APP_NAME)
        self.last_dir = str(self.settings.value('last_dir', os.path.expanduser('~')))
        self.ports = core.list_serial_ports()
        self._port_keys = [p.key() for p in self.ports]

        self.ifaces = []
        tb = self.addToolBar('Yeni')
        tb.setMovable(False)
        newbtn = W.QToolButton()
        newbtn.setText('  + Yeni sekme  ')
        newbtn.setPopupMode(qenum(W.QToolButton, 'ToolButtonPopupMode.InstantPopup'))
        nm = W.QMenu(newbtn)
        for i, cls in enumerate(ALL_TYPES):
            if i == len(SESSION_TYPES):
                nm.addSeparator()
            nm.addAction(cls.TITLE).triggered.connect(lambda _=False, c=cls: self.add_session(c))
        newbtn.setMenu(nm)
        tb.addWidget(newbtn)
        tb.addSeparator()
        for cls in (SerialSession, TcpClientSession, PingTab, IperfTab):
            tb.addAction('+ ' + cls.TITLE).triggered.connect(lambda _=False, c=cls: self.add_session(c))
        tb.addSeparator()
        setb = W.QToolButton()
        setb.setText('  Ayarlar  ')
        setb.setPopupMode(qenum(W.QToolButton, 'ToolButtonPopupMode.InstantPopup'))
        sm = W.QMenu(setb)
        self.act_admin = sm.addAction('Acilista yonetici izni iste (IP degistirme, seri port izni)')
        self.act_admin.setCheckable(True)
        self.act_admin.setChecked(str(self.settings.value('ask_admin', 'true')).lower() in ('1', 'true'))
        self.act_admin.toggled.connect(lambda v: self.settings.setValue('ask_admin', v))
        sm.addAction('Yonetici iznini simdi iste').triggered.connect(self.request_admin)
        setb.setMenu(sm)
        tb.addWidget(setb)
        tb.addAction('Hakkinda').triggered.connect(self.about)

        self.tabs = W.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)

        for cls in ALL_TYPES:
            self.add_session(cls, focus=False)
        self.tabs.setCurrentIndex(0)

        self.statusBar().showMessage(f'{len(self.ports)} seri port bulundu   |   Qt: {QT_API}', 5000)
        self.lbl_admin = W.QLabel()
        self.statusBar().addPermanentWidget(self.lbl_admin)
        self.lbl_ports = W.QLabel()
        self.statusBar().addPermanentWidget(self.lbl_ports)
        self._update_port_label()

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
        for i in range(self.tabs.count()):
            self.tabs.widget(i).update_ports(ports)
        self._update_port_label()

    def _update_port_label(self):
        self.lbl_ports.setText(f'Seri portlar: {len(self.ports)}  ')
        self.lbl_ports.setToolTip('\n'.join(p.label for p in self.ports) or 'Port yok')

    # -- sekmeler
    def add_session(self, cls, focus=True):
        s = cls(self)
        idx = self.tabs.addTab(s, s.tab_label())
        if focus:
            self.tabs.setCurrentIndex(idx)
        return s

    def close_tab(self, idx):
        w = self.tabs.widget(idx)
        if w.transport is not None or getattr(w, 'is_busy', lambda: False)():
            r = W.QMessageBox.question(self, 'Sekmeyi kapat', 'Baglanti / test calisiyor. Kapatilsin mi?')
            if r != qenum(W.QMessageBox, 'StandardButton.Yes'):
                return
        w.shutdown()
        self.tabs.removeTab(idx)
        w.deleteLater()

    def session_state_changed(self, s):
        idx = self.tabs.indexOf(s)
        if idx < 0:
            return
        self.tabs.setTabText(idx, ('● ' if s.connected else '') + s.tab_label())
        self.tabs.tabBar().setTabTextColor(idx, QtGui.QColor('#2da44e') if s.connected
                                           else self.palette().color(qenum(QtGui.QPalette, 'ColorRole.WindowText')))

    # -- ag araclari arasi baglantilar
    def ifaces_changed(self, ifaces):
        self.ifaces = ifaces
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, ScanTab):
                w.ifaces_changed(ifaces)

    def _first(self, cls):
        for i in range(self.tabs.count()):
            if isinstance(self.tabs.widget(i), cls):
                self.tabs.setCurrentIndex(i)
                return self.tabs.widget(i)
        return self.add_session(cls)

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
            return True, '<span style="color:#2da44e">🔓 Yonetici: aktif</span>'
        h = net.PrivHelper.instance
        if h is not None and h.alive:
            return True, '<span style="color:#2da44e">🔓 Yonetici izni: verildi</span>'
        return False, '<span style="color:#888">🔒 Yonetici izni yok (IP degisikliginde sorulur)</span>'

    def _admin_changed(self):
        self.lbl_admin.setText(self.admin_state()[1] + '  ')
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
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
        W.QMessageBox.about(
            self, f'{APP_NAME} {APP_VERSION}',
            f'<b>{APP_NAME} {APP_VERSION}</b><br>Seri port / TCP / UDP hizli test ve izleme araci.<br><br>'
            f'Python {sys.version.split()[0]} &nbsp; Qt arayuz: {QT_API} &nbsp; pyserial {serial.VERSION}<br>'
            f'Ayarlar: {self.settings.fileName()}')

    def closeEvent(self, ev):
        if net.PrivHelper.instance is not None:
            net.PrivHelper.instance.close()
        self._scan_stop.set()
        self._scan_now.set()
        self.settings.setValue('geometry', self.saveGeometry())
        self.settings.setValue('last_dir', self.last_dir)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
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


def main():
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
    ask_admin = str(QtCore.QSettings(APP_NAME, APP_NAME).value('ask_admin', 'true')).lower() in ('1', 'true')
    if (core.IS_WIN and ask_admin and not selftest and not net.is_admin()
            and '--elevated' not in sys.argv):
        # acilista BIR KEZ UAC: onaylanirsa yonetici olarak yeniden baslar, reddedilirse normal devam
        if net.relaunch_as_admin():
            sys.exit(0)
    app.setApplicationName(APP_NAME)
    app.setStyle('Fusion')
    icon = resource('icon.png')
    if os.path.exists(icon):
        app.setWindowIcon(QtGui.QIcon(icon))
    win = MainWindow()
    _st('ana pencere olustu')
    win.show()
    win._admin_changed()
    if selftest:
        QtCore.QTimer.singleShot(300, lambda: _selftest(win))
    elif ask_admin and not core.IS_WIN and not net.is_admin():
        QtCore.QTimer.singleShot(400, win.request_admin)
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
    print(f'{APP_NAME} selftest: pencere acildi', flush=True)
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
