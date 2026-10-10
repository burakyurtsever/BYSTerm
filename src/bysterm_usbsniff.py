"""
Windows'ta seri port dinleme: SANAL PORT YOK, portu kullanan uygulama hic degismez.

USB-seri ceviriciler (FTDI, CP210x, CH340/CH341, PL2303, CDC-ACM: Arduino, STM32 VCP, ESP32,
Pico...) icin USBPcap kullanilir: USBPcap, USB surucu yiginina takilan imzali bir filtre
surucusudur ve USB veri yolundan gecen her paketi kopyalar. Bu akistan cihazin seri verisini
(RX/TX), baud/format ayarlarini ve DTR/RTS gibi hat degisikliklerini cikaririz.

    USBPcapCMD.exe -d \\\\.\\USBPcap1 --devices 5 --inject-descriptors -o -   ->  pcap (DLT 249) stdout

Bu modul Qt'den bagimsizdir; cozumleyiciler Linux'ta da test edilir.
"""

import os
import re
import struct
import subprocess
import sys
import time

import bysterm_core as core

IS_WIN = sys.platform.startswith('win')
DLT_USBPCAP = 249
USBPCAP_VERSION = '1.5.4.0'
USBPCAP_SETUP_URL = ('https://github.com/desowin/usbpcap/releases/download/'
                     f'{USBPCAP_VERSION}/USBPcapSetup-{USBPCAP_VERSION}.exe')
USBPCAP_SHA256 = '87a7edf9bbbcf07b5f4373d9a192a6770d2ff3add7aa1e276e82e38582ccb622'   # resmi, imzali kurulum
USBPCAP_PAGE = 'https://desowin.org/usbpcap/'

# USBPcap paket basligi (USBPcap.h, pack 1): headerLen, irpId, status, function, info, bus, device,
# endpoint, transfer, dataLength  -> 27 bayt. Kontrol aktariminda ardindan 1 bayt "stage".
_HDR = struct.Struct('<HQIHBHHBBI')
INFO_PDO_TO_FDO = 1                       # 1 = cihazdan donus (tamamlanma), 0 = gonderim
TR_ISO, TR_INT, TR_CTRL, TR_BULK = 0, 1, 2, 3

_NO_WINDOW = 0x08000000 if IS_WIN else 0


# =========================================================================== pcap akisi
class PcapStream:
    """Parca parca gelen pcap (klasik veya pcapng) akisini paketlere ayirir. feed() -> [(ts, linktype, veri)]"""

    def __init__(self):
        self.buf = bytearray()
        self.kind = None          # 'pcap' | 'pcapng'
        self.endian = '<'
        self.link = None
        self.ns = False
        self.ng_links = []

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            if self.kind is None:
                if len(self.buf) < 24:
                    break
                magic = bytes(self.buf[:4])
                if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1'):
                    self.kind, self.endian = 'pcap', '<'
                elif magic in (b'\xa1\xb2\xc3\xd4', b'\xa1\xb2\x3c\x4d'):
                    self.kind, self.endian = 'pcap', '>'
                elif magic == b'\x0a\x0d\x0d\x0a':
                    self.kind = 'pcapng'
                    continue
                else:
                    raise ValueError('pcap akisi degil (bilinmeyen baslik)')
                self.ns = magic in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d')
                self.link = struct.unpack(self.endian + 'I', bytes(self.buf[20:24]))[0]
                del self.buf[:24]
                continue
            if self.kind == 'pcap':
                if len(self.buf) < 16:
                    break
                sec, frac, incl, _orig = struct.unpack(self.endian + 'IIII', bytes(self.buf[:16]))
                if incl > 16 * 1024 * 1024:
                    raise ValueError('bozuk pcap kaydi')
                if len(self.buf) < 16 + incl:
                    break
                pkt = bytes(self.buf[16:16 + incl])
                del self.buf[:16 + incl]
                out.append((sec + frac / (1e9 if self.ns else 1e6), self.link, pkt))
                continue
            # pcapng
            if len(self.buf) < 12:
                break
            if bytes(self.buf[:4]) == b'\x0a\x0d\x0d\x0a':            # SHB: bayt sirasi burada belirlenir
                self.endian = '<' if bytes(self.buf[8:12]) == b'\x4d\x3c\x2b\x1a' else '>'
            btype, blen = struct.unpack(self.endian + 'II', bytes(self.buf[:8]))
            if blen < 12 or blen > 32 * 1024 * 1024:
                raise ValueError('bozuk pcapng blogu')
            if len(self.buf) < blen:
                break
            body = bytes(self.buf[8:blen - 4])
            del self.buf[:blen]
            if btype == 1:                                   # IDB
                self.ng_links.append(struct.unpack(self.endian + 'H', body[:2])[0])
            elif btype == 6:                                 # EPB
                iface, th, tl, cap, _orig = struct.unpack(self.endian + 'IIIII', body[:20])
                link = self.ng_links[iface] if iface < len(self.ng_links) else DLT_USBPCAP
                out.append((((th << 32) | tl) / 1e6, link, body[20:20 + cap]))
            elif btype == 3:                                 # SPB
                link = self.ng_links[0] if self.ng_links else DLT_USBPCAP
                out.append((time.time(), link, body[4:]))
        return out


def parse_usbpcap(pkt):
    """USBPcap paketini sozluge cevir; bozuksa None."""
    if len(pkt) < _HDR.size:
        return None
    hl, irp, status, func, info, bus, dev, ep, tr, dlen = _HDR.unpack_from(pkt, 0)
    if hl < _HDR.size or hl > len(pkt):
        return None
    stage = pkt[_HDR.size] if tr == TR_CTRL and hl > _HDR.size else None
    return {'irp': irp, 'status': status, 'func': func, 'done': bool(info & INFO_PDO_TO_FDO),
            'bus': bus, 'dev': dev, 'ep': ep, 'tr': tr, 'stage': stage,
            'data': pkt[hl:hl + dlen]}


# =========================================================================== cip cozumleri
PARITY = {0: 'N', 1: 'O', 2: 'E', 3: 'M', 4: 'S'}
STOPS = {0: '1', 1: '1.5', 2: '2'}
STD_BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200,
             128000, 153600, 230400, 250000, 256000, 460800, 500000, 576000, 921600, 1000000,
             1500000, 2000000, 3000000, 4000000, 6000000, 12000000)


def nice_baud(b):
    """FTDI/CH340 bolenleri tam sayi vermez (115384 gibi) -> %3 icindeyse standart degeri goster."""
    if not b:
        return b
    best = min(STD_BAUDS, key=lambda s: abs(s - b))
    return best if abs(best - b) <= best * 0.03 else int(round(b))


def chip_of(vid, pid=0):
    return {0x0403: 'ftdi', 0x10C4: 'cp210x', 0x1A86: 'ch34x', 0x067B: 'pl2303'}.get(vid, 'cdc')


CHIP_NAMES = {'ftdi': 'FTDI', 'cp210x': 'Silicon Labs CP210x', 'ch34x': 'WCH CH340/CH341',
              'pl2303': 'Prolific PL2303', 'cdc': 'USB CDC (sanal COM)'}

# FTDI: 3 bitlik kesir kodu -> sekizde kac (ftdi_sio divfrac tablosunun tersi)
_FTDI_FRAC = {0: 0, 1: 4, 2: 2, 3: 1, 4: 3, 5: 5, 6: 6, 7: 7}


def ftdi_baud(wvalue, windex, bcd=0x600):
    hi_chip = bcd in (0x500, 0x700, 0x800, 0x900)       # coklu port / H serisi: wIndex = (bolen>>16)<<8 | port
    hi = (windex >> 8) if hi_chip else windex
    enc = wvalue | ((hi & 0xFF) << 16)
    base = 12000000 if (enc & 0x20000) and bcd in (0x700, 0x800, 0x900) else 3000000
    whole = enc & 0x3FFF
    eighths = _FTDI_FRAC[(enc >> 14) & 7]
    if base == 3000000 and whole == 0 and eighths == 0:
        return 3000000
    if base == 3000000 and whole == 1 and eighths == 0:
        return 2000000
    div = whole + eighths / 8.0
    return int(round(base / div)) if div else 0


def ch34x_baud(val):
    """CH341 0x1312 yazmaci: (0x100-div)<<8 | fact<<2 | ps   (Linux ch341.c ile ayni bicim)."""
    ps = val & 3
    fact = (val >> 2) & 1
    div = 0x100 - ((val >> 8) & 0xFF)
    if div <= 0:
        return 0
    clk_div = 1 << (12 - 3 * ps - fact)
    return int(round(48000000.0 / (clk_div * div)))


def ch34x_lcr(lcr):
    bits = 5 + (lcr & 3)
    par = 'N'
    if lcr & 0x08:
        par = {0: 'O', 1: 'E', 2: 'M', 3: 'S'}[(lcr >> 4) & 3]
    stop = '2' if lcr & 0x04 else '1'
    return f'{bits}{par}{stop}'


class _Dev:
    def __init__(self, addr):
        self.addr = addr
        self.vid = self.pid = self.bcd = None
        self.chip = None
        self.eps = {}          # endpoint adresi -> (maxpacket, arayuz)
        self.n_serial_if = 0
        self.line = {}         # son bilinen ayarlar
        self.mstat = {}


class UsbSerialDecoder:
    """USBPcap paketlerinden seri olaylari uretir:
        ('rx', bayt, etiket)   cihaz -> PC   (uygulamanin okudugu)
        ('tx', bayt, etiket)   PC -> cihaz   (uygulamanin yazdigi)
        ('info', metin, seviye)
    address verilirse sadece o USB adresindeki cihaz; vid/pid verilirse ilk eslesen cihaz izlenir."""

    def __init__(self, address=None, vid=None, pid=None):
        self.address = address
        self.want = (vid, pid) if vid else None
        self.devs = {}
        self.setups = {}       # (dev, irp) -> setup paketi (veri cihazdan donecekse)
        self.announced = set()

    def _dev(self, addr):
        d = self.devs.get(addr)
        if d is None:
            d = self.devs[addr] = _Dev(addr)
        return d

    def _target(self, addr):
        if self.address is not None:
            return addr == self.address
        if self.want:
            d = self.devs.get(addr)
            if d is not None and d.vid == self.want[0] and (self.want[1] is None or d.pid == self.want[1]):
                self.address = addr          # kilitlen
                return True
            return False
        return True

    # -- tanimlayicilar (USBPcapCMD --inject-descriptors ya da canli numaralandirma)
    def _descriptor(self, d, setup, data):
        dtype = setup[3]
        if dtype == 1 and len(data) >= 18:
            d.vid, d.pid, d.bcd = struct.unpack_from('<HHH', data, 8)
            d.chip = chip_of(d.vid, d.pid)
        elif dtype == 2 and len(data) >= 9:
            i, iface, icls = 0, 0, None
            n_if = 0
            while i + 2 <= len(data):
                ln, typ = data[i], data[i + 1]
                if ln < 2:
                    break
                if typ == 4 and i + 6 <= len(data):
                    iface, icls = data[i + 2], data[i + 5]
                    if icls in (0x0A, 0xFF):
                        n_if += 1
                elif typ == 5 and i + 6 <= len(data):
                    ep = data[i + 2]
                    mps = struct.unpack_from('<H', data, i + 4)[0] & 0x7FF
                    d.eps[ep] = (mps or 64, iface)
                i += ln
            d.n_serial_if = n_if

    def _announce(self, d, out):
        if d.addr in self.announced or d.vid is None:
            return
        self.announced.add(d.addr)
        out.append(('info', f'USB cihaz: {CHIP_NAMES.get(d.chip, d.chip)}  '
                            f'(VID {d.vid:04X} PID {d.pid:04X}, adres {d.addr})', 'info'))

    def _set_line(self, d, out, **kv):
        changed = {k: v for k, v in kv.items() if v is not None and d.line.get(k) != v}
        if not changed:
            return
        d.line.update(changed)
        parts = []
        if 'baud' in d.line:
            parts.append(f'{d.line["baud"]} baud')
        if 'fmt' in d.line:
            parts.append(d.line['fmt'])
        if 'flow' in d.line:
            parts.append(d.line['flow'])
        out.append(('info', 'Uygulama port ayari: ' + '  '.join(parts), 'info'))

    def _lines(self, d, out, **kv):
        changed = {k: v for k, v in kv.items() if v is not None and d.mstat.get(k) != v}
        if not changed:
            return
        d.mstat.update(changed)
        out.append(('info', 'Hat: ' + '  '.join(f'{k}={"1" if v else "0"}' for k, v in sorted(d.mstat.items())),
                    'info'))

    # -- kontrol istekleri (port ayarlari)
    def _control(self, d, s, data, out):
        rtype, req = s[0], s[1]
        wv, wi, _wl = struct.unpack_from('<HHH', s, 2)
        if rtype & 0x60 == 0x20 and req in (0x20,) and len(data) >= 7:        # CDC / PL2303 SET_LINE_CODING
            rate, stop, par, bits = struct.unpack_from('<IBBB', data, 0)
            self._set_line(d, out, baud=nice_baud(rate),
                           fmt=f'{bits}{PARITY.get(par, "?")}{STOPS.get(stop, "?")}')
        elif rtype & 0x60 == 0x20 and req == 0x22:                              # SET_CONTROL_LINE_STATE
            self._lines(d, out, DTR=bool(wv & 1), RTS=bool(wv & 2))
        elif rtype & 0x60 == 0x20 and req == 0x23:                              # SEND_BREAK
            out.append(('info', 'BREAK gonderildi', 'warn'))
        elif d.chip == 'ftdi' and rtype == 0x40:
            if req == 3:
                self._set_line(d, out, baud=nice_baud(ftdi_baud(wv, wi, d.bcd or 0x600)))
            elif req == 4:
                bits, par, stop = wv & 0xFF, (wv >> 8) & 7, (wv >> 11) & 3
                self._set_line(d, out, fmt=f'{bits}{PARITY.get(par, "?")}{STOPS.get(stop, "?")}')
                if wv & 0x4000:
                    out.append(('info', 'BREAK gonderildi', 'warn'))
            elif req == 1:
                kv = {}
                if wv & 0x0100:
                    kv['DTR'] = bool(wv & 1)
                if wv & 0x0200:
                    kv['RTS'] = bool(wv & 2)
                self._lines(d, out, **kv)
            elif req == 2:
                flow = {0: None, 1: 'RTS/CTS', 2: 'DTR/DSR', 4: 'XON/XOFF'}.get(wi >> 8)
                self._set_line(d, out, flow=flow or 'akis yok')
        elif d.chip == 'cp210x' and rtype == 0x41:
            if req == 0x1E and len(data) >= 4:
                self._set_line(d, out, baud=nice_baud(struct.unpack_from('<I', data, 0)[0]))
            elif req == 0x01 and wv:
                self._set_line(d, out, baud=nice_baud(3686400 // wv))
            elif req == 0x03:
                self._set_line(d, out, fmt=f'{wv >> 8}{PARITY.get((wv >> 4) & 0xF, "?")}{STOPS.get(wv & 0xF, "?")}')
            elif req == 0x07:
                kv = {}
                if wv & 0x0100:
                    kv['DTR'] = bool(wv & 1)
                if wv & 0x0200:
                    kv['RTS'] = bool(wv & 2)
                self._lines(d, out, **kv)
            elif req == 0x05 and wv:
                out.append(('info', 'BREAK gonderildi', 'warn'))
        elif d.chip == 'ch34x' and rtype == 0x40:
            if req in (0x9A, 0xA1):
                regs = {}
                if req == 0x9A:
                    regs[(wv & 0xFF, wv >> 8)] = (wi & 0xFF, wi >> 8)
                else:                                                            # SERIAL_INIT: wIndex = baud yazmaci
                    if wi:
                        regs[(0x12, 0x13)] = (wi & 0xFF, wi >> 8)
                    if wv & 0xFF00:
                        regs[(0x18, 0x25)] = (wv >> 8, 0)
                for (ra, rb), (va, vb) in regs.items():
                    if (ra, rb) == (0x12, 0x13):
                        self._set_line(d, out, baud=nice_baud(ch34x_baud(va | (vb << 8))))
                    elif (ra, rb) == (0x18, 0x25):
                        self._set_line(d, out, fmt=ch34x_lcr(va))
            elif req == 0xA4:                                                    # modem kontrol (ters mantik)
                self._lines(d, out, DTR=not (wv & 0x20), RTS=not (wv & 0x40))

    # -- ana giris
    def feed(self, p):
        out = []
        if p is None:
            return out
        dev = p['dev']
        d = self._dev(dev)
        if p['tr'] == TR_CTRL:
            key = (dev, p['irp'])
            if not p['done'] and p['stage'] in (0, None) and len(p['data']) >= 8:
                s = p['data'][:8]
                wlen = struct.unpack_from('<H', s, 6)[0]
                if len(self.setups) > 4096:
                    self.setups.clear()
                if s[0] & 0x80:                          # veri cihazdan donecek -> tamamlanmayi bekle
                    self.setups[key] = s
                elif wlen and len(p['data']) == 8:       # eski USBPcap: OUT verisi ayri "data" asamasinda gelir
                    self.setups[('out',) + key] = s
                elif self._target(dev):
                    self._control(d, s, p['data'][8:], out)
            elif not p['done'] and p['stage'] == 1 and ('out',) + key in self.setups:
                s = self.setups.pop(('out',) + key)
                if self._target(dev):
                    self._control(d, s, p['data'], out)
            elif p['done'] and key in self.setups and p['data']:
                s = self.setups.pop(key)
                if s[1] == 6:                            # GET_DESCRIPTOR
                    self._descriptor(d, s, p['data'])
                    if self._target(dev):
                        self._announce(d, out)
                elif self._target(dev):
                    self._control(d, s, p['data'], out)
            elif p['done']:
                self.setups.pop(key, None)
                self.setups.pop(('out',) + key, None)
            return out
        if not self._target(dev):
            return out
        self._announce(d, out)
        data = p['data']
        if not data or p['status'] not in (0,):
            return out
        ep = p['ep']
        mps, iface = d.eps.get(ep, (64, 0))
        tag = f'if{iface}' if d.n_serial_if > 1 else ''
        if p['tr'] == TR_BULK:
            if ep & 0x80:
                if not p['done']:
                    return out
                if d.chip == 'ftdi':
                    data = self._ftdi_in(d, data, mps, out)
                if data:
                    out.append(('rx', bytes(data), tag))
            elif not p['done']:
                out.append(('tx', bytes(data), tag))
        elif p['tr'] == TR_INT and ep & 0x80 and p['done'] and d.chip == 'cdc':
            # CDC SERIAL_STATE bildirimi: A1 20 ... + 2 bayt durum
            if len(data) >= 10 and data[0] == 0xA1 and data[1] == 0x20:
                st = struct.unpack_from('<H', data, 8)[0]
                self._lines(d, out, DCD=bool(st & 1), DSR=bool(st & 2))
                for bit, name in ((0x10, 'cerceve hatasi'), (0x20, 'parite hatasi'), (0x40, 'tasma')):
                    if st & bit:
                        out.append(('info', f'Hat hatasi: {name}', 'warn'))
        return out

    def _ftdi_in(self, d, data, mps, out):
        """FTDI: her maxpacket parcasinin ilk 2 bayti durumdur (modem, hat) -> ayikla."""
        res = bytearray()
        for i in range(0, len(data), mps):
            chunk = data[i:i + mps]
            if len(chunk) < 2:
                continue
            modem, line = chunk[0], chunk[1]
            self._lines(d, out, CTS=bool(modem & 0x10), DSR=bool(modem & 0x20))
            for bit, name in ((0x02, 'tasma'), (0x04, 'parite hatasi'), (0x08, 'cerceve hatasi'),
                              (0x10, 'BREAK alindi')):
                if line & bit and len(chunk) > 2:
                    out.append(('info', f'Hat hatasi: {name}', 'warn'))
            res += chunk[2:]
        return res


# =========================================================================== USBPcap yonetimi
def usbpcap_cmd():
    """USBPcapCMD.exe yolu (komut listesi) ya da None. BYSTERM_USBPCAPCMD ile degistirilebilir (test)."""
    ov = os.environ.get('BYSTERM_USBPCAPCMD')
    if ov:
        return [sys.executable, ov] if ov.endswith('.py') else [ov]
    cands = []
    for env in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)'):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, 'USBPcap', 'USBPcapCMD.exe'),
                      os.path.join(base, 'Wireshark', 'extcap', 'USBPcapCMD.exe')]
    for c in cands:
        if os.path.isfile(c):
            return [c]
    return None


def usbpcap_installed():
    return usbpcap_cmd() is not None


def _run(cmd, timeout=15):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                       creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)
    return p.stdout.decode('utf-8', 'replace')


_IF_RE = re.compile(r'interface\s*\{value=([^}]*)\}')
_VAL_RE = re.compile(r'value\s*\{arg=\d+\}\{value=(\d+)(?:_(\d+))?\}\{display=([^}]*)\}')


def parse_extcap_interfaces(text):
    return [m.group(1) for m in _IF_RE.finditer(text)]


def parse_extcap_devices(text):
    """--extcap-config ciktisi -> {adres: {'name': ..., 'children': [ad, ...]}}.
    Alt dugumler (cocuk aygitlar) arkadaki COM adini tasir: 'USB Serial Port (COM5)'."""
    devs = {}
    for m in _VAL_RE.finditer(text):
        addr, node, disp = int(m.group(1)), m.group(2), m.group(3)
        d = devs.setdefault(addr, {'name': '', 'children': []})
        if node is None:
            d['name'] = re.sub(r'^\[\d+\]\s*', '', disp)
        else:
            d['children'].append(disp)
    return devs


def com_in_name(com, name):
    return re.search(r'\(\s*%s\s*\)' % re.escape(com), name or '', re.I) is not None


def find_port(com, cmd=None):
    """COM portunun hangi USBPcap kokunde, hangi USB adresinde oldugunu bul -> (arayuz, adres, ad) | None."""
    cmd = cmd or usbpcap_cmd()
    if not cmd:
        return None
    for iface in parse_extcap_interfaces(_run(cmd + ['--extcap-interfaces'])):
        devs = parse_extcap_devices(_run(cmd + ['--extcap-config', '--extcap-interface', iface]))
        for addr, d in devs.items():
            if com_in_name(com, d['name']) or any(com_in_name(com, c) for c in d['children']):
                return iface, addr, d['name']
    return None


class UsbSerialSniffer(core.Transport):
    """Windows: bir COM portunun (USB-seri cevirici) trafigini USBPcap ile pasif dinler.
    Portu acan uygulama hic degismez; sanal port yok; BYSTerm porta hic dokunmaz."""
    kind = 'usbsniff'
    labels = {core.RX: 'CIHAZ>', core.TX: 'UYGUL>'}

    def __init__(self, port, iface=None, address=None, vid=None, pid=None):
        super().__init__()
        self.port = port
        self.iface, self.address = iface, address
        self.vid, self.pid = vid, pid
        self.proc = None
        self.description = f'USB · {port}'

    def _open_and_run(self):
        cmd = usbpcap_cmd()
        if not cmd:
            raise RuntimeError('USBPcap kurulu degil. "USB dinleme surucusunu kur" ile bir kez kurun '
                               've Windows\'u yeniden baslatin.')
        if self.iface is None:
            hit = find_port(self.port, cmd)
            if not hit:
                raise RuntimeError(f'{self.port} bir USB-seri cevirici olarak bulunamadi. USBPcap yeni '
                                   'kurulduysa Windows\'u yeniden baslatin; yerlesik (anakart) COM portlari '
                                   'icin "Sanal port koprusu" yontemini kullanin.')
            self.iface, self.address, name = hit
            self._info(f'{self.port}: {name} (USB adres {self.address}, {self.iface})')
        args = cmd + ['-d', self.iface, '-o', '-', '-s', '65535', '-b', '1048576', '--inject-descriptors']
        if self.address is not None:
            args += ['--devices', str(self.address)]
        else:
            args += ['-A']
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, bufsize=0, creationflags=_NO_WINDOW)
        errs = []
        self._spawn(self._stderr_loop, errs, name='err')
        stream = PcapStream()
        dec = UsbSerialDecoder(self.address, self.vid, self.pid)
        self._state('open', f'Dinleme basladi: {self.port} (USB). Portu kullanan uygulama hic degismez; '
                            f'sanal port yok. (Pasif dinleme)')
        got_header = False
        while not self._stop.is_set():
            chunk = self.proc.stdout.read(65536)
            if not chunk:
                break
            got_header = True
            for _ts, link, pkt in stream.feed(chunk):
                if link != DLT_USBPCAP:
                    continue
                for ev in dec.feed(parse_usbpcap(pkt)):
                    if ev[0] == 'rx':
                        self._data(core.RX, ev[1], ev[2])
                    elif ev[0] == 'tx':
                        self._data(core.TX, ev[1], ev[2])
                    else:
                        self._info(ev[1], ev[2])
        if not self._stop.is_set():
            time.sleep(0.2)
            msg = ' '.join(errs).strip()
            if not got_header and ('denied' in msg.lower() or 'elevat' in msg.lower() or not msg):
                msg = msg or 'USBPcap baslatilamadi (yonetici izni reddedildi mi?)'
            self._fail(f'Dinleme durdu: {msg}' if msg else 'Dinleme durdu (cihaz cikarildi mi?)')

    def _stderr_loop(self, errs):
        for line in iter(self.proc.stderr.readline, b''):
            t = line.decode('utf-8', 'replace').strip()
            if t:
                errs.append(t)
                del errs[:-5]

    def send(self, data, target=None):
        self._info('Dinleme modunda gonderme yok (pasif)', 'warn')

    def _interrupt(self):
        self._release()

    def _release(self):
        p = self.proc
        if p is None:
            return
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except Exception:
                pass


def usbpcap_install(progress=None):
    """USBPcap'in resmi (imzali) kurulumunu GitHub'dan indirip baslatir. -> (basarili, metin). Windows."""
    if not IS_WIN:
        return False, 'Yalniz Windows'
    import tempfile
    import urllib.request
    import ssl
    import ctypes
    from ctypes import wintypes
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        tmp = tempfile.mkdtemp(prefix='bysterm_usbpcap_')
        exe = os.path.join(tmp, f'USBPcapSetup-{USBPCAP_VERSION}.exe')
        req = urllib.request.Request(USBPCAP_SETUP_URL, headers={'User-Agent': 'BYSTerm'})
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r, open(exe, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            done = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        import hashlib
        with open(exe, 'rb') as f:
            blob = f.read()
        if blob[:2] != b'MZ' or hashlib.sha256(blob).hexdigest() != USBPCAP_SHA256:
            return False, 'Indirilen dosya bir kurulum programi degil'

        class SEI(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                        ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR),
                        ('lpParameters', wintypes.LPCWSTR), ('lpDirectory', wintypes.LPCWSTR),
                        ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                        ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR),
                        ('hkeyClass', wintypes.HKEY), ('dwHotKey', wintypes.DWORD),
                        ('hIconOrMonitor', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]
        sei = SEI()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = 0x00000040
        sei.lpVerb = 'runas'
        sei.lpFile = exe
        sei.lpParameters = ''
        sei.lpDirectory = tmp
        sei.nShow = 1
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            err = ctypes.GetLastError()
            return (False, 'Iptal edildi (UAC)') if err == 1223 else (False, f'Kurulum baslatilamadi ({err})')
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        if usbpcap_installed():
            return True, 'USBPcap kuruldu. Dinlemenin calismasi icin Windows\'u bir kez yeniden baslatin.'
        return False, 'Kurulum tamamlanmadi'
    except Exception as e:     # noqa: BLE001
        return False, f'Hata: {e}'
