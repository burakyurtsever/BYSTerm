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
import time
import socket
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
from bysterm_core import RX, TX  # noqa: E402

APP_NAME = 'BYSTerm'
APP_VERSION = '1.0.0'

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


SESSION_TYPES = [SerialSession, TcpClientSession, TcpServerSession, UdpSession, MonitorSession]


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

        tb = self.addToolBar('Yeni')
        tb.setMovable(False)
        tb.addWidget(W.QLabel('  Yeni sekme: '))
        for cls in SESSION_TYPES:
            act = tb.addAction('+ ' + cls.TITLE)
            act.triggered.connect(lambda _=False, c=cls: self.add_session(c))
        tb.addSeparator()
        tb.addAction('Hakkinda').triggered.connect(self.about)

        self.tabs = W.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)

        for cls in SESSION_TYPES:
            self.add_session(cls, focus=False)
        self.tabs.setCurrentIndex(0)

        self.statusBar().showMessage(f'{len(self.ports)} seri port bulundu   |   Qt: {QT_API}', 5000)
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
        if w.transport is not None:
            r = W.QMessageBox.question(self, 'Sekmeyi kapat', 'Baglanti acik. Kapatilsin mi?')
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

    def about(self):
        W.QMessageBox.about(
            self, f'{APP_NAME} {APP_VERSION}',
            f'<b>{APP_NAME} {APP_VERSION}</b><br>Seri port / TCP / UDP hizli test ve izleme araci.<br><br>'
            f'Python {sys.version.split()[0]} &nbsp; Qt arayuz: {QT_API} &nbsp; pyserial {serial.VERSION}<br>'
            f'Ayarlar: {self.settings.fileName()}')

    def closeEvent(self, ev):
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
        _t = threading.Timer(60, _early)
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
    app.setApplicationName(APP_NAME)
    app.setStyle('Fusion')
    icon = resource('icon.png')
    if os.path.exists(icon):
        app.setWindowIcon(QtGui.QIcon(icon))
    win = MainWindow()
    _st('ana pencere olustu')
    win.show()
    if '--selftest' in sys.argv:
        QtCore.QTimer.singleShot(300, lambda: _selftest(win))
    sys.exit(qexec(app))


def _selftest(win):
    """Paketlenmis uygulamanin bu sistemde calistigini dogrular (CI ve kullanici icin):
    pencere acilir, kendi icinde TCP sunucu <-> istemci veri alisverisi yapilir."""
    step = {'n': 0}
    names = ('sunucu aciliyor', 'istemci baglaniyor', 'veri bekleniyor')

    def watchdog():   # olay dongusu kilitlense bile surec mutlaka biter
        print(f'{APP_NAME} SELFTEST TIMEOUT (adim: {names[min(step["n"], 2)]})', flush=True)
        os._exit(3)
    wd = threading.Timer(30, watchdog)
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
            wd.cancel()
            ports = len(win.ports)
            print(f'{APP_NAME} {APP_VERSION} SELFTEST {"OK" if ok else "FAIL"} '
                  f'(Qt {QtCore.qVersion()} / {QT_API}, Python {sys.version.split()[0]}, '
                  f'{sys.platform}, seri port: {ports})', flush=True)
            win.close()
            W.QApplication.instance().exit(0 if ok else 1)
            threading.Timer(5, lambda: os._exit(0 if ok else 1)).start()   # kapanis takilirsa
            return
        QtCore.QTimer.singleShot(50, poll)
    poll()


if __name__ == '__main__':
    main()
