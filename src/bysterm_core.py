#!/usr/bin/env python3
"""
BYSTerm cekirdegi — Qt'den BAGIMSIZ katman (transportlar, bicimleyici, port tarama).

Tasarim (yuksek veri akisinda donmamak icin):
  * Her baglanti kendi is parcaciklarinda (thread) okur/yazar; GUI hicbir zaman
    soket/port uzerinde BLOKLANMAZ.
  * Is parcaciklari olaylari kilitsiz bir deque'ya atar:
        ('data',   ts, yon, bytes, peer)
        ('info',   ts, seviye, metin)        seviye: info | warn | error
        ('state',  ts, durum, metin)         durum : open | client+ | client- | app+ | app-
        ('closed', ts, sebep)
  * GUI bir zamanlayici ile kuyrugu toplu bosaltir; sayaclar/kayit HER baytı gorur,
    ekran ise tik basina bir butceyle sinirlanir (bkz. bysterm.py).

Bagimlilik: pyserial (pip install pyserial)
"""
import os
import sys
import time
import glob
import queue
import socket
import select
import datetime
import threading
import collections

import serial
import serial.tools.list_ports

IS_WIN = sys.platform.startswith('win')
IS_POSIX = os.name == 'posix'

RX, TX = 'RX', 'TX'

STANDARD_BAUDS = [300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400,
                  57600, 76800, 115200, 128000, 230400, 250000, 256000, 460800,
                  500000, 576000, 921600, 1000000, 1500000, 2000000, 3000000]

PARITIES = {'Yok (N)': serial.PARITY_NONE, 'Cift (E)': serial.PARITY_EVEN,
            'Tek (O)': serial.PARITY_ODD, 'Mark (M)': serial.PARITY_MARK,
            'Space (S)': serial.PARITY_SPACE}
STOPBITS = {'1': serial.STOPBITS_ONE, '1.5': serial.STOPBITS_ONE_POINT_FIVE,
            '2': serial.STOPBITS_TWO}
FLOWS = ['Yok', 'RTS/CTS', 'XON/XOFF', 'DSR/DTR']


# --------------------------------------------------------------------------- port tarama
# pyserial'in Linux taramasi bazi gomulu UART'lari listelemez (Jetson: ttyTHS*, ttyTCU*;
# i.MX: ttymxc*; Samsung: ttySAC*; TI: ttyO*). Onlari elle ekliyoruz.
_EXTRA_LINUX_GLOBS = ['/dev/ttyTHS*', '/dev/ttyTCU*', '/dev/ttymxc*', '/dev/ttySAC*',
                      '/dev/ttyO[0-9]*', '/dev/ttyMSM*', '/dev/ttyLP*']


class PortInfo:
    __slots__ = ('device', 'description', 'hwid', 'manufacturer', 'serial_number', 'vid', 'pid')

    def __init__(self, device, description='', hwid='', manufacturer='', serial_number='',
                 vid=None, pid=None):
        self.device = device
        self.description = description if description and description != 'n/a' else ''
        self.hwid = hwid if hwid and hwid != 'n/a' else ''
        self.manufacturer = manufacturer or ''
        self.serial_number = serial_number or ''
        self.vid, self.pid = vid, pid

    @property
    def label(self):
        d = self.description
        if d and d != self.device and not d.startswith(self.device):
            s = f'{self.device}  —  {d}'
        else:
            s = self.device
        if self.vid is not None and self.pid is not None:
            s += f'  [{self.vid:04X}:{self.pid:04X}]'
        return s

    @property
    def tooltip(self):
        parts = [self.device]
        for k, v in (('Aciklama', self.description), ('Uretici', self.manufacturer),
                     ('Seri no', self.serial_number), ('HWID', self.hwid)):
            if v:
                parts.append(f'{k}: {v}')
        return '\n'.join(parts)

    def key(self):
        return (self.device, self.description, self.hwid)


def _port_sort_key(dev):
    # COM3 < COM10, ttyUSB2 < ttyUSB10
    head = dev.rstrip('0123456789')
    tail = dev[len(head):]
    return (head, int(tail) if tail else -1)


def list_serial_ports():
    """Sistemdeki seri portlari isimleri/aciklamalariyla dondurur (PortInfo listesi)."""
    found = {}
    try:
        for p in serial.tools.list_ports.comports():
            found[p.device] = PortInfo(p.device, p.description, p.hwid,
                                       getattr(p, 'manufacturer', ''),
                                       getattr(p, 'serial_number', ''),
                                       getattr(p, 'vid', None), getattr(p, 'pid', None))
    except Exception:
        pass
    if sys.platform.startswith('linux'):
        for pat in _EXTRA_LINUX_GLOBS:
            for dev in glob.glob(pat):
                if dev not in found:
                    found[dev] = PortInfo(dev, 'Gomulu UART')
    elif sys.platform == 'darwin':
        # macOS: comports() cu.* ve tty.* ikilisini verir; cu.* olani yeterli/dogru olan.
        found = {k: v for k, v in found.items() if not k.startswith('/dev/tty.')} or found
    return sorted(found.values(), key=lambda p: _port_sort_key(p.device))


# --------------------------------------------------------------------------- yardimcilar
def parse_hex(text):
    """'48 65 6C', '48656C', '0x48,0x65', '48-65:6c' -> bytes. Hatalida ValueError."""
    t = text.replace('0x', ' ').replace('0X', ' ')
    for ch in ',;:-_\t\r\n':
        t = t.replace(ch, ' ')
    out = bytearray()
    for tok in t.split():
        if len(tok) % 2:
            if len(tok) == 1:
                tok = '0' + tok
            else:
                raise ValueError(f'Tek sayida hex hane: "{tok}"')
        try:
            out += bytes.fromhex(tok)
        except ValueError:
            raise ValueError(f'Gecersiz hex: "{tok}"') from None
    return bytes(out)


def parse_escapes(text):
    r"""ASCII metindeki \r \n \t \0 \\ \xHH kacislarini cozer (latin-1 disi -> UTF-8)."""
    out = bytearray()
    i, n = 0, len(text)
    simple = {'r': 13, 'n': 10, 't': 9, '0': 0, '\\': 92, 'e': 27, 'a': 7, 'b': 8}
    while i < n:
        c = text[i]
        if c == '\\' and i + 1 < n:
            nx = text[i + 1]
            if nx in simple:
                out.append(simple[nx])
                i += 2
                continue
            if nx in 'xX' and i + 3 < n + 1:
                h = text[i + 2:i + 4]
                try:
                    out.append(int(h, 16))
                    i += 4
                    continue
                except ValueError:
                    pass
        out += c.encode('utf-8')
        i += 1
    return bytes(out)


def human_bytes(n):
    for unit in ('B', 'kB', 'MB', 'GB'):
        if abs(n) < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024.0


# --------------------------------------------------------------------------- bicimleyici
def _build_ascii_table(ctrl_hex=True):
    tbl = {}
    for b in range(256):
        if b in (9, 10):
            continue                       # \t ve \n oldugu gibi
        if 32 <= b < 127:
            continue
        tbl[b] = f'<{b:02X}>' if ctrl_hex else '.'
    return tbl


try:
    b''.hex(' ')

    def hexsp(b):
        return b.hex(' ').upper()
except TypeError:                       # Python < 3.8 (Ubuntu 18.04 / JetPack 4)
    import binascii

    def hexsp(b):
        h = binascii.hexlify(b).decode('ascii').upper()
        return ' '.join(h[i:i + 2] for i in range(0, len(h), 2))


_ASCII_HEX = _build_ascii_table(True)
_ASCII_DOT = _build_ascii_table(False)
_DUMP_TBL = {b: '.' for b in range(256) if not (32 <= b < 127)}

MODES = ('ascii', 'hex', 'dump')


class Formatter:
    """Ham veri parcalarini ekranda/kayitta gosterilecek metin segmentlerine cevirir.

    format() -> [(tur, metin), ...]  tur: 'hdr' | RX | TX | 'info' | 'error' | 'warn'
    Durum tutar: ayni yon/peer'den gelen ardisik veri (zaman damgasi kapaliyken) ayni
    satirda devam eder; HEX modunda satir genisligi korunur.
    """
    MAX_LINE = 2000      # ASCII'de cok uzun satiri kir (QPlainTextEdit performansi)

    def __init__(self, mode='ascii', timestamps=True, hex_width=16, ctrl_hex=True,
                 labels=None):
        self.mode = mode
        self.timestamps = timestamps
        self.hex_width = hex_width
        self.ctrl_hex = ctrl_hex
        self.labels = labels or {RX: 'RX', TX: 'TX'}
        self.reset()

    def reset(self):
        self.last_key = None
        self.line_open = False   # imlec bir satirin ortasinda mi
        self.col = 0             # hex: satirdaki bayt; ascii: satirdaki karakter
        self.pending_cr = False
        self.indent = ''

    def _header(self, ts, direction, peer):
        h = ''
        if self.timestamps:
            h += datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S.%f')[:-3] + ' '
        h += self.labels.get(direction, direction)
        if peer:
            h += f' {peer}'
        return h + ' '

    def text_line(self, ts, kind, text):
        """Bilgi/hata satiri (her zaman kendi satirinda)."""
        pre = '\n' if self.line_open else ''
        stamp = datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S.%f')[:-3]
        self.last_key = None
        self.line_open = False
        self.pending_cr = False
        return [(kind, f'{pre}{stamp} -- {text}\n')]

    def format(self, ts, direction, data, peer=''):
        if not data:
            return []
        segs = []
        key = (direction, peer)
        new_line = self.timestamps or key != self.last_key or self.mode == 'dump'
        if new_line:
            hdr = self._header(ts, direction, peer)
            segs.append(('hdr', ('\n' if self.line_open else '') + hdr))
            self.indent = ' ' * len(hdr)
            self.col = 0
            self.line_open = True
            self.pending_cr = False
        self.last_key = key

        if self.mode == 'hex':
            segs.append((direction, self._hex(data, new_line)))
        elif self.mode == 'dump':
            segs.append((direction, self._dump(data)))
            self.line_open = False
            self.col = 0
        else:
            segs.append((direction, self._ascii(data)))
        return segs

    # -- ascii
    def _ascii(self, data):
        if self.pending_cr and data[:1] == b'\n':
            data = data[1:]
        self.pending_cr = data.endswith(b'\r')
        t = data.decode('latin-1')
        t = t.replace('\r\n', '\n').replace('\r', '\n')
        t = t.translate(_ASCII_HEX if self.ctrl_hex else _ASCII_DOT)
        # cok uzun (satir sonu olmayan) akisi kir
        last_nl = t.rfind('\n')
        if last_nl < 0:
            if self.col + len(t) > self.MAX_LINE:
                parts = []
                room = self.MAX_LINE - self.col
                parts.append(t[:room])
                rest = t[room:]
                while rest:
                    parts.append(rest[:self.MAX_LINE])
                    rest = rest[self.MAX_LINE:]
                t = '\n'.join(parts)
                self.col = len(parts[-1])
            else:
                self.col += len(t)
        else:
            tail = len(t) - last_nl - 1
            if tail > self.MAX_LINE or (self.col + last_nl) > self.MAX_LINE:
                t = '\n'.join(self._wrap(line) for line in t.split('\n'))
                tail = len(t) - t.rfind('\n') - 1
            self.col = tail
        return t

    def _wrap(self, line):
        if len(line) <= self.MAX_LINE:
            return line
        return '\n'.join(line[i:i + self.MAX_LINE] for i in range(0, len(line), self.MAX_LINE))

    # -- hex (akista satir genisligi korunur)
    def _hex(self, data, fresh):
        w = self.hex_width
        out = []
        pos = 0
        if not fresh and self.col:
            if self.col >= w:
                out.append('\n' + self.indent)
                self.col = 0
            else:
                take = data[:w - self.col]
                out.append(' ' + hexsp(take))
                self.col += len(take)
                pos = len(take)
        while pos < len(data):
            if self.col >= w:
                out.append('\n' + self.indent)
                self.col = 0
            take = data[pos:pos + (w - self.col)]
            out.append((' ' if self.col else '') + hexsp(take))
            self.col += len(take)
            pos += len(take)
        return ''.join(out)

    # -- hex + ascii dokumu
    def _dump(self, data):
        w = self.hex_width
        lines = [f'({len(data)} bayt)']
        for off in range(0, len(data), w):
            chunk = data[off:off + w]
            hx = hexsp(chunk)
            asc = chunk.decode('latin-1').translate(_DUMP_TBL)
            lines.append(f'  {off:04X}  {hx:<{w * 3 - 1}}  |{asc}|')
        return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- transport tabani
class Transport:
    """Tum baglanti turlerinin ortak tabani. start() asenkron acar, close() kapatir."""
    kind = 'base'
    labels = {RX: 'RX', TX: 'TX'}

    def __init__(self):
        self.events = collections.deque()
        self._stop = threading.Event()
        self._threads = []
        self._txq = queue.Queue()
        self._closed_emitted = False
        self._lock = threading.Lock()
        self.description = ''

    # -- olaylar
    def _data(self, direction, data, peer=''):
        self.events.append(('data', time.time(), direction, data, peer))

    def _info(self, text, level='info'):
        self.events.append(('info', time.time(), level, text))

    def _state(self, state, text=''):
        self.events.append(('state', time.time(), state, text))

    def _fail(self, reason):
        """Is parcacigindan cagrilir: baglanti kendiliginden koptu."""
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            if not self._closed_emitted:
                self._closed_emitted = True
                self.events.append(('closed', time.time(), reason))
        try:
            self._release()
        except Exception:
            pass

    # -- thread yonetimi
    def _spawn(self, fn, *args, name='io'):
        t = threading.Thread(target=self._guard, args=(fn,) + args, daemon=True,
                             name=f'{self.kind}-{name}')
        self._threads.append(t)
        t.start()
        return t

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:     # noqa: BLE001 — her hata kullaniciya gosterilir
            if not self._stop.is_set():
                self._fail(_err_text(e))

    def _writer(self):
        while not self._stop.is_set():
            try:
                item = self._txq.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            data, target = item
            self._write(data, target)

    # -- alt siniflar
    def start(self):
        self._spawn(self._open_and_run, name='main')

    def _open_and_run(self):
        raise NotImplementedError

    def _write(self, data, target):
        raise NotImplementedError

    def _release(self):
        """Kaynaklari kapat (thread'leri join ETMEZ)."""

    def send(self, data, target=None):
        if data and not self._stop.is_set():
            self._txq.put((bytes(data), target))

    def close(self):
        with self._lock:
            already = self._stop.is_set()
            self._stop.set()
            self._closed_emitted = True
        self._txq.put(None)
        if not already:
            try:
                self._release()
            except Exception:
                pass
        cur = threading.current_thread()
        for t in self._threads:
            if t is not cur:
                t.join(timeout=1.5)

    @property
    def running(self):
        return not self._stop.is_set()


def _err_text(e):
    if isinstance(e, PermissionError) or 'Permission denied' in str(e) or 'Erisim engellendi' in str(e):
        hint = ''
        if sys.platform.startswith('linux'):
            hint = ("  (Linux: kullaniciyi 'dialout' grubuna ekleyin: "
                    "sudo usermod -aG dialout $USER ; sonra oturumu kapatip acin)")
        elif IS_WIN:
            hint = '  (Port baska bir uygulamada acik olabilir.)'
        return f'Erisim reddedildi: {e}{hint}'
    if isinstance(e, socket.timeout):
        return 'Zaman asimi'
    if isinstance(e, ConnectionRefusedError):
        return 'Baglanti reddedildi (karsida dinleyen yok)'
    return f'{type(e).__name__}: {e}'


# --------------------------------------------------------------------------- seri port
class SerialConfig:
    def __init__(self, port, baudrate=115200, bytesize=8, parity=serial.PARITY_NONE,
                 stopbits=serial.STOPBITS_ONE, flow='Yok', dtr=True, rts=True):
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = int(bytesize)
        self.parity = parity
        self.stopbits = stopbits
        self.flow = flow
        self.dtr = dtr
        self.rts = rts

    def short(self):
        sb = {1: '1', 1.5: '1.5', 2: '2'}.get(self.stopbits, str(self.stopbits))
        return f'{self.port} {self.baudrate} {self.bytesize}{self.parity}{sb}'

    def open(self, timeout=0.05):
        s = serial.Serial()
        s.port = self.port
        s.baudrate = self.baudrate
        s.bytesize = self.bytesize
        s.parity = self.parity
        s.stopbits = self.stopbits
        s.timeout = timeout
        s.write_timeout = 2.0
        s.rtscts = self.flow == 'RTS/CTS'
        s.xonxoff = self.flow == 'XON/XOFF'
        s.dsrdtr = self.flow == 'DSR/DTR'
        if self.flow != 'RTS/CTS':
            s.rts = self.rts
        if self.flow != 'DSR/DTR':
            s.dtr = self.dtr
        if IS_POSIX:
            s.exclusive = True     # Linux/mac: baska bir surec ayni portu acamasin
        s.open()
        return s


def serial_read_packet(ser, stop, gap, max_chunk, max_wait=0.05):
    """Bir 'paket' oku: ilk bayti bekle, sonra hat `gap` sn sessiz kalana kadar topla.

    Ekranda paketlerin bolunmeden (Hercules/Eltima gibi) gorunmesini saglar; surekli
    akista max_wait/max_chunk sinirlari gecikmeyi dusuk tutar.
    """
    first = ser.read(max(1, min(ser.in_waiting, max_chunk)))
    if not first:
        return b''
    buf = bytearray(first)
    t0 = last = time.monotonic()
    while len(buf) < max_chunk and not stop.is_set():
        n = ser.in_waiting
        now = time.monotonic()
        if n:
            buf += ser.read(min(n, max_chunk - len(buf)))
            last = now
        elif now - last >= gap:
            break
        else:
            time.sleep(0.0005)
        if now - t0 >= max_wait:
            break
    return bytes(buf)


def _gap_for_baud(baud):
    # ~3 karakter suresi, en az 2 ms (USB-seri cevirici gecikmeleri icin)
    return max(0.002, 30.0 / max(1, baud))


def _chunk_for_baud(baud):
    return max(64, min(16384, int(baud) // 200))


class SerialTransport(Transport):
    kind = 'serial'

    def __init__(self, cfg: SerialConfig):
        super().__init__()
        self.cfg = cfg
        self.ser = None
        self.description = cfg.short()

    def _open_and_run(self):
        self.ser = self.cfg.open()
        self._state('open', f'{self.cfg.short()} acildi')
        self._spawn(self._writer, name='tx')
        gap, chunk = _gap_for_baud(self.cfg.baudrate), _chunk_for_baud(self.cfg.baudrate)
        while not self._stop.is_set():
            try:
                data = serial_read_packet(self.ser, self._stop, gap, chunk)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if not self._stop.is_set():
                    self._fail(f'Port baglantisi koptu: {e}')
                return
            if data:
                self._data(RX, data)

    def _write(self, data, target):
        try:
            self.ser.write(data)
        except serial.SerialTimeoutException:
            self._info('Yazma zaman asimi (akis kontrolu karsi tarafca durdurulmus olabilir)', 'warn')
            return
        except Exception as e:     # noqa: BLE001
            self._fail(f'Yazma hatasi: {e}')
            return
        self._data(TX, data)

    def _release(self):
        s = self.ser
        if s is not None:
            for fn in (s.cancel_read, s.cancel_write):
                try:
                    fn()
                except Exception:
                    pass
            try:
                s.close()
            except Exception:
                pass

    # kontrol hatlari (GUI'den cagrilir)
    def set_dtr(self, v):
        if self.ser and self.ser.is_open:
            self.ser.dtr = v

    def set_rts(self, v):
        if self.ser and self.ser.is_open:
            self.ser.rts = v

    def send_break(self, dur=0.25):
        if self.ser and self.ser.is_open:
            self.ser.send_break(dur)

    def modem_lines(self):
        s = self.ser
        if not (s and s.is_open):
            return None
        try:
            return {'CTS': s.cts, 'DSR': s.dsr, 'DCD': s.cd, 'RI': s.ri}
        except Exception:
            return None


# --------------------------------------------------------------------------- TCP istemci
class TcpClientTransport(Transport):
    kind = 'tcpc'

    def __init__(self, host, port, connect_timeout=5.0):
        super().__init__()
        self.host, self.port = host, int(port)
        self.timeout = connect_timeout
        self.sock = None
        self.description = f'{host}:{port}'

    def _open_and_run(self):
        self._info(f'{self.host}:{self.port} adresine baglaniliyor...')
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(0.3)
        self.sock = s
        if self._stop.is_set():
            s.close()
            return
        local = s.getsockname()
        self._state('open', f'Baglandi: {self.host}:{self.port}  (yerel {local[0]}:{local[1]})')
        self._spawn(self._writer, name='tx')
        while not self._stop.is_set():
            try:
                data = s.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                if not self._stop.is_set():
                    self._fail(f'Baglanti hatasi: {e}')
                return
            if not data:
                self._fail('Karsi taraf baglantiyi kapatti')
                return
            self._data(RX, data)

    def _write(self, data, target):
        try:
            self.sock.sendall(data)
        except OSError as e:
            self._fail(f'Gonderme hatasi: {e}')
            return
        self._data(TX, data)

    def _release(self):
        s = self.sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


# --------------------------------------------------------------------------- TCP sunucu
class TcpServerTransport(Transport):
    kind = 'tcps'

    def __init__(self, bind_host, port):
        super().__init__()
        self.bind_host, self.port = bind_host or '0.0.0.0', int(port)
        self.lsock = None
        self.clients = {}        # 'ip:port' -> socket
        self._clock = threading.Lock()
        self.description = f'{self.bind_host}:{self.port}'

    def _open_and_run(self):
        fam = socket.AF_INET6 if ':' in self.bind_host else socket.AF_INET
        ls = socket.socket(fam, socket.SOCK_STREAM)
        if not IS_WIN:   # Windows'ta SO_REUSEADDR portu "calmaya" izin verir; kullanma
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((self.bind_host, self.port))
        ls.listen(16)
        ls.settimeout(0.3)
        self.lsock = ls
        self.port = ls.getsockname()[1]
        self._state('open', f'Dinleniyor: {self.bind_host}:{self.port}')
        self._spawn(self._writer, name='tx')
        while not self._stop.is_set():
            try:
                cs, addr = ls.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if not self._stop.is_set():
                    self._fail(f'Dinleme hatasi: {e}')
                return
            peer = f'{addr[0]}:{addr[1]}'
            cs.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            cs.settimeout(0.3)
            with self._clock:
                self.clients[peer] = cs
            self._state('client+', peer)
            self._spawn(self._client_loop, peer, cs, name=f'c{peer}')

    def _client_loop(self, peer, cs):
        reason = 'baglanti kapandi'
        try:
            while not self._stop.is_set():
                try:
                    data = cs.recv(65536)
                except socket.timeout:
                    continue
                except OSError as e:
                    reason = str(e)
                    break
                if not data:
                    reason = 'istemci kapatti'
                    break
                self._data(RX, data, peer)
        finally:
            with self._clock:
                self.clients.pop(peer, None)
            try:
                cs.close()
            except OSError:
                pass
            if not self._stop.is_set():
                self._state('client-', f'{peer} ({reason})')

    def _write(self, data, target):
        with self._clock:
            if target:
                targets = [(p, self.clients[p]) for p in target if p in self.clients]
            else:
                targets = list(self.clients.items())
        if not targets:
            self._info('Gonderilecek bagli istemci yok', 'warn')
            return
        for peer, cs in targets:
            try:
                cs.sendall(data)
                self._data(TX, data, peer)
            except OSError as e:
                self._info(f'{peer}: gonderme hatasi: {e}', 'warn')

    def kick(self, peer):
        with self._clock:
            cs = self.clients.get(peer)
        if cs:
            try:
                cs.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _release(self):
        if self.lsock:
            try:
                self.lsock.close()
            except OSError:
                pass
        with self._clock:
            socks = list(self.clients.values())
        for cs in socks:
            try:
                cs.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                cs.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- UDP
class UdpTransport(Transport):
    kind = 'udp'

    def __init__(self, local_host, local_port, remote_host='', remote_port=0, broadcast=False):
        super().__init__()
        self.local_host = local_host or '0.0.0.0'
        self.local_port = int(local_port or 0)
        self.remote = (remote_host, int(remote_port)) if remote_host and remote_port else None
        self.broadcast = broadcast
        self.sock = None
        self.description = f'udp {self.local_host}:{self.local_port}'

    def _open_and_run(self):
        fam = socket.AF_INET6 if ':' in self.local_host else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_DGRAM)
        if not IS_WIN:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.broadcast:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        s.bind((self.local_host, self.local_port))
        s.settimeout(0.3)
        self.sock = s
        self.local_port = s.getsockname()[1]
        rem = f'  hedef {self.remote[0]}:{self.remote[1]}' if self.remote else ''
        self._state('open', f'UDP dinleniyor: {self.local_host}:{self.local_port}{rem}')
        self._spawn(self._writer, name='tx')
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows: onceki gonderim ICMP 'port unreachable' aldi -> yok say
                continue
            except OSError as e:
                if not self._stop.is_set():
                    self._fail(f'UDP hatasi: {e}')
                return
            self._data(RX, data, f'{addr[0]}:{addr[1]}')

    def _write(self, data, target):
        dest = target or self.remote
        if not dest:
            self._info('Hedef adres/port girilmedi', 'warn')
            return
        try:
            self.sock.sendto(data, dest)
        except OSError as e:
            self._info(f'Gonderme hatasi ({dest[0]}:{dest[1]}): {e}', 'warn')
            return
        self._data(TX, data, f'{dest[0]}:{dest[1]}')

    def _release(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- seri izleme (kopru)
_POSIX_BAUDS = {}
if IS_POSIX:
    import termios
    import tty
    import errno
    for _b in [50, 75, 110, 134, 150, 200, 300, 600, 1200, 1800, 2400, 4800, 9600, 19200,
               38400, 57600, 115200, 230400, 460800, 500000, 576000, 921600, 1000000,
               1152000, 1500000, 2000000, 2500000, 3000000, 3500000, 4000000]:
        _c = getattr(termios, f'B{_b}', None)
        if _c is not None:
            _POSIX_BAUDS[_c] = _b


class _PtyEnd:
    """Linux/macOS: sanal seri port. Diger uygulama `link` yolunu (symlink) acar."""

    def __init__(self, link):
        self.link = link
        self.master, slave = os.openpty()
        self.slave_name = os.ttyname(slave)
        tty.setraw(slave)
        os.close(slave)              # slave'i tutmuyoruz -> uygulama acti/kapatti algilanir
        os.set_blocking(self.master, False)
        self._made_link = False
        if link and link != self.slave_name:
            if os.path.islink(link):
                os.unlink(link)
            elif os.path.exists(link):
                os.close(self.master)
                raise FileExistsError(f'{link} zaten var ve bir symlink degil')
            os.symlink(self.slave_name, link)
            self._made_link = True
        self.initial = self.termios_settings()   # uygulama acmadan onceki varsayilan

    def close(self):
        try:
            os.close(self.master)
        except OSError:
            pass
        if self._made_link:
            try:
                if os.path.islink(self.link):
                    os.unlink(self.link)
            except OSError:
                pass

    def termios_settings(self):
        """Uygulamanin pty'ye uyguladigi (baud, bits, parity, stop) — Linux/macOS."""
        a = termios.tcgetattr(self.master)
        cflag = a[2]
        baud = _POSIX_BAUDS.get(a[5], a[5])
        size = {termios.CS5: 5, termios.CS6: 6, termios.CS7: 7, termios.CS8: 8}.get(
            cflag & termios.CSIZE, 8)
        if cflag & termios.PARENB:
            parity = serial.PARITY_ODD if cflag & termios.PARODD else serial.PARITY_EVEN
        else:
            parity = serial.PARITY_NONE
        stop = serial.STOPBITS_TWO if cflag & termios.CSTOPB else serial.STOPBITS_ONE
        return baud, size, parity, stop


class SerialBridge(Transport):
    """Eltima tarzi izleme: [Cihaz] <-> GERCEK PORT <-BYSTerm-> SANAL PORT <-> [Uygulama]

    Diger uygulama gercek port yerine sanal portu acar; BYSTerm iki yonu de iletir ve
    gosterir. Linux/macOS'ta sanal port otomatik (pty) olusturulur. Windows'ta bir sanal
    null-modem cifti gerekir (com0com vb.): BYSTerm ciftin bir ucunu acar, uygulama
    diger ucu acar.

    passive=True: iletim YOK, iki (gercek) port sadece dinlenir — iki USB-seri ceviricinin
    RX uclari hattin TX/RX'ine baglanarak yapilan donanim 'tap'i icin.
    """
    kind = 'bridge'
    labels = {RX: 'CIHAZ>', TX: 'UYGUL>'}

    def __init__(self, cfg: SerialConfig, virtual, follow=True, passive=False):
        super().__init__()
        self.cfg = cfg
        self.virtual = virtual.strip()
        self.follow = follow
        self.passive = passive
        self.ser = None
        self.pty = None
        self.vser = None
        self.app_connected = False
        self.dropped = 0
        if passive:
            self.labels = {RX: 'A>', TX: 'B>'}
        self.description = f'{cfg.port} <-> {self.virtual}'

    def _use_pty(self):
        if not IS_POSIX or self.passive:
            return False
        try:
            import stat
            st = os.stat(self.virtual)
            return not stat.S_ISCHR(st.st_mode)
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _open_and_run(self):
        if not self.virtual:
            raise ValueError('Sanal/ikinci port belirtilmedi')
        self.ser = self.cfg.open()
        try:
            if self._use_pty():
                self.pty = _PtyEnd(self.virtual)
                where = self.virtual if self.virtual else self.pty.slave_name
                self._state('open', f'Izleme basladi: {self.cfg.short()}  <->  sanal port {where} '
                                    f'(-> {self.pty.slave_name}). Diger uygulamada "{where}" portunu acin.')
            else:
                vcfg = SerialConfig(self.virtual, self.cfg.baudrate, self.cfg.bytesize,
                                    self.cfg.parity, self.cfg.stopbits)
                self.vser = vcfg.open()
                self.app_connected = True
                if self.passive:
                    self._state('open', f'Pasif dinleme: A={self.cfg.port}  B={self.virtual} '
                                        f'@{self.cfg.baudrate}')
                else:
                    self._state('open', f'Izleme basladi: {self.cfg.short()} <-> {self.virtual}. '
                                        f'Diger uygulamada sanal ciftin OBUR ucunu acin.')
        except Exception:
            self.ser.close()
            raise
        self._spawn(self._app_side, name='app')
        if self.pty and self.follow:
            self._spawn(self._follow_loop, name='follow')
        gap, chunk = _gap_for_baud(self.cfg.baudrate), _chunk_for_baud(self.cfg.baudrate)
        while not self._stop.is_set():
            try:
                data = serial_read_packet(self.ser, self._stop, gap, chunk)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if not self._stop.is_set():
                    self._fail(f'Gercek port koptu: {e}')
                return
            if not data:
                continue
            self._data(RX, data)
            if not self.passive:
                self._to_app(data)

    def _to_app(self, data):
        if self.pty:
            if not self.app_connected:
                # Linux: anlik kontrol (yoklama araligini bekleme -> ilk baytlar kaybolmaz).
                # macOS'ta acik/kapali bilinemez -> yine de yazmayi dene.
                if self._pty_slave_open():
                    self._mark_app_connected()
                elif sys.platform.startswith('linux'):
                    self.dropped += len(data)
                    return
            try:
                os.write(self.pty.master, data)
            except BlockingIOError:
                self.dropped += len(data)
                self._info(f'Uygulama veriyi okumuyor; {len(data)} bayt atildi', 'warn')
            except OSError:
                self.dropped += len(data)
        elif self.vser:
            try:
                self.vser.write(data)
            except Exception as e:      # noqa: BLE001
                self._info(f'Sanal porta yazma hatasi: {e}', 'warn')

    def _app_side(self):
        if self.pty:
            self._pty_loop()
        else:
            self._vser_loop()

    def _pty_loop(self):
        m = self.pty.master
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([m], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                data = os.read(m, 65536)
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno == errno.EIO:          # slave acik degil (uygulama bagli degil)
                    if self.app_connected:
                        self.app_connected = False
                        self._state('app-', 'Uygulama sanal portu kapatti')
                    self._stop.wait(0.1)
                    continue
                if not self._stop.is_set():
                    self._fail(f'Sanal port hatasi: {e}')
                return
            if not data:                        # macOS: slave kapali -> 0 bayt
                if self.app_connected:
                    self.app_connected = False
                    self._state('app-', 'Uygulama sanal portu kapatti')
                self._stop.wait(0.1)
                continue
            if not self.app_connected:
                self._mark_app_connected()
            self._from_app(data)

    def _mark_app_connected(self):
        with self._lock:
            if self.app_connected:
                return
            self.app_connected = True
        msg = 'Uygulama sanal portu acti'
        if self.dropped:
            msg += f' (bu arada cihazdan gelen {self.dropped} bayt uygulamaya iletilmedi)'
            self.dropped = 0
        self._state('app+', msg)

    def _vser_loop(self):
        gap, chunk = _gap_for_baud(self.cfg.baudrate), _chunk_for_baud(self.cfg.baudrate)
        while not self._stop.is_set():
            try:
                data = serial_read_packet(self.vser, self._stop, gap, chunk)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if not self._stop.is_set():
                    self._fail(f'Ikinci port koptu: {e}')
                return
            if data:
                self._from_app(data)

    def _from_app(self, data):
        self._data(TX, data)
        if self.passive:
            return
        try:
            self.ser.write(data)
        except Exception as e:          # noqa: BLE001
            self._info(f'Gercek porta yazma hatasi: {e}', 'warn')

    def _follow_loop(self):
        """Uygulamanin pty'de sectigi baud/format'i gercek porta uygular (Linux/macOS).

        Uygulamanin bagli olup olmadigini da buradan anlariz: pty'ye ilk kez yazmadan
        once de acmis olabilir (sadece dinleyen uygulama) — termios degisimi / HUP yoklanir.
        """
        last = self.pty.initial
        while not self._stop.wait(0.3):
            if not self.app_connected and self._pty_slave_open():
                self._mark_app_connected()
            try:
                cur = self.pty.termios_settings()
            except Exception:
                continue
            if cur != last:
                last = cur
                baud, size, parity, stop = cur
                try:
                    if isinstance(baud, int) and baud > 0 and baud != self.ser.baudrate:
                        self.ser.baudrate = baud
                    if size != self.ser.bytesize:
                        self.ser.bytesize = size
                    if parity != self.ser.parity:
                        self.ser.parity = parity
                    if stop != self.ser.stopbits:
                        self.ser.stopbits = stop
                    sb = '2' if stop == serial.STOPBITS_TWO else '1'
                    self._info(f'Uygulama port ayarini degistirdi -> gercek port: '
                               f'{baud} {size}{parity}{sb}')
                except Exception as e:      # noqa: BLE001
                    self._info(f'Uygulamanin ayari ({baud}) gercek porta uygulanamadi: {e}', 'warn')

    def _pty_slave_open(self):
        """Linux: slave hic acik degilse master'da POLLHUP olur. (macOS'ta poll() tty'lerde
        guvenilmez -> orada uygulama ilk veriyi yazinca algilanir.)"""
        if not sys.platform.startswith('linux'):
            return False
        try:
            p = select.poll()
            p.register(self.pty.master, select.POLLHUP)
            ev = p.poll(0)
        except (OSError, ValueError):
            return False
        return not any(e & select.POLLHUP for _, e in ev)

    def _release(self):
        for s in (self.ser, self.vser):
            if s is not None:
                for fn in (s.cancel_read, s.cancel_write):
                    try:
                        fn()
                    except Exception:
                        pass
                try:
                    s.close()
                except Exception:
                    pass
        if self.pty:
            self.pty.close()

