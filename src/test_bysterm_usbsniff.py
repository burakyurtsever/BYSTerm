"""bysterm_usbsniff testleri: USBPcap akisindan seri veri/ayar cozumleme + sahte USBPcapCMD ile uctan uca.

    python3 src/test_bysterm_usbsniff.py
"""
import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bysterm_core as core          # noqa: E402
import bysterm_usbsniff as u         # noqa: E402

_irp = [1000]


def rec(dev, ep, tr, done, data=b'', stage=None, status=0, irp=None, bus=1):
    """Tek USBPcap paketi (USBPcap.h ile ayni yerlesim)."""
    if irp is None:
        _irp[0] += 1
        irp = _irp[0]
    hl = 28 if tr == u.TR_CTRL else 27
    h = struct.pack('<HQIHBHHBBI', hl, irp, status, 9, 1 if done else 0, bus, dev, ep, tr, len(data))
    if tr == u.TR_CTRL:
        h += bytes([stage if stage is not None else (3 if done else 0)])
    return h + data


def pcap(pkts):
    out = struct.pack('<IHHiIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 249)
    for i, p in enumerate(pkts):
        out += struct.pack('<IIII', 1700000000 + i, 0, len(p), len(p)) + p
    return out


def setup(rtype, req, wv=0, wi=0, wl=0):
    return struct.pack('<BBHHH', rtype, req, wv, wi, wl)


def descriptors(dev, vid, pid, bcd=0x0600, ep_in=0x81, ep_out=0x02, mps=64, icls=0xFF, n_if=1):
    """Cihaz + yapilandirma tanimlayicisi (USBPcapCMD --inject-descriptors gibi)."""
    dd = struct.pack('<BBHBBBBHHHBBBB', 18, 1, 0x200, 0, 0, 0, 64, vid, pid, bcd, 1, 2, 3, 1)
    body = b''
    for i in range(n_if):
        body += struct.pack('<BBBBBBBBB', 9, 4, i, 0, 2, icls, 0, 0, 0)
        body += struct.pack('<BBBBHB', 7, 5, ep_in + 2 * i, 2, mps, 0)
        body += struct.pack('<BBBBHB', 7, 5, ep_out + 2 * i, 2, mps, 0)
    cd = struct.pack('<BBHBBBBB', 9, 2, 9 + len(body), n_if, 1, 0, 0x80, 50) + body
    out = []
    for typ, data in ((1, dd), (2, cd)):
        _irp[0] += 1
        out.append(rec(dev, 0x80, u.TR_CTRL, False, setup(0x80, 6, typ << 8, 0, len(data)), irp=_irp[0]))
        out.append(rec(dev, 0x80, u.TR_CTRL, True, data, irp=_irp[0]))
    return out


def ctrl_out(dev, rtype, req, wv=0, wi=0, data=b'', old_style=False):
    s = setup(rtype, req, wv, wi, len(data))
    _irp[0] += 1
    i = _irp[0]
    if old_style and data:       # USBPcap < 1.5: setup ve veri ayri paketlerde
        return [rec(dev, 0, u.TR_CTRL, False, s, stage=0, irp=i), rec(dev, 0, u.TR_CTRL, False, data, stage=1, irp=i),
                rec(dev, 0, u.TR_CTRL, True, b'', stage=2, irp=i)]
    return [rec(dev, 0, u.TR_CTRL, False, s + data, irp=i), rec(dev, 0, u.TR_CTRL, True, b'', irp=i)]


def decode(pkts, **kw):
    dec = u.UsbSerialDecoder(**kw)
    s = u.PcapStream()
    raw = pcap(pkts)
    evs = []
    for i in range(0, len(raw), 13):            # akisi bilerek kucuk parcalarla besle
        for _ts, link, p in s.feed(raw[i:i + 13]):
            assert link == 249
            evs += dec.feed(u.parse_usbpcap(p))
    return evs


def stream(evs, kind):
    return b''.join(e[1] for e in evs if e[0] == kind)


def infos(evs):
    return [e[1] for e in evs if e[0] == 'info']


class Decoders(unittest.TestCase):
    def test_ftdi_bulk_strips_status_per_packet(self):
        d1 = bytes([0x01, 0x60]) + b'A' * 62
        d2 = bytes([0x01, 0x60]) + b'hello'
        pk = descriptors(5, 0x0403, 0x6001) + [
            rec(5, 0x81, u.TR_BULK, True, bytes([0x01, 0x60])),           # bos yoklama: sadece durum
            rec(5, 0x81, u.TR_BULK, True, d1 + d2),                        # iki 64'luk paket tek URB'de
            rec(5, 0x02, u.TR_BULK, False, b'AT\r\n'),
            rec(5, 0x02, u.TR_BULK, True, b''),                            # OUT tamamlanmasi: veri yok
            rec(5, 0x81, u.TR_BULK, True, b'zz', status=0xC0000120),       # iptal: atla
        ]
        evs = decode(pk, address=5)
        self.assertEqual(stream(evs, 'rx'), b'A' * 62 + b'hello')
        self.assertEqual(stream(evs, 'tx'), b'AT\r\n')
        self.assertTrue(any('FTDI' in t for t in infos(evs)))

    def test_ftdi_line_settings(self):
        pk = descriptors(5, 0x0403, 0x6001) + \
            ctrl_out(5, 0x40, 3, 0x001a) + ctrl_out(5, 0x40, 4, 0x0008) + ctrl_out(5, 0x40, 1, 0x0101) + \
            ctrl_out(5, 0x40, 3, 0x4138) + ctrl_out(5, 0x40, 4, 0x0108 | (2 << 11)) + ctrl_out(5, 0x40, 2, 0, 0x0100)
        t = infos(decode(pk, address=5))
        self.assertIn('Uygulama port ayari: 115200 baud  8N1', t)
        self.assertIn('Hat: DTR=1', t)
        self.assertTrue(any('9600 baud' in x for x in t), t)                # 0x4138 = 312.5 -> 9600
        self.assertTrue(any('8O2' in x and 'RTS/CTS' in x for x in t), t)

    def test_ftdi_baud_table(self):
        self.assertEqual(u.ftdi_baud(0x09c4, 0), 1200)
        self.assertEqual(u.nice_baud(u.ftdi_baud(0x001a, 0)), 115200)
        self.assertEqual(u.ftdi_baud(0, 0), 3000000)
        self.assertEqual(u.ftdi_baud(1, 0), 2000000)
        self.assertEqual(u.nice_baud(u.ftdi_baud(0x8003, 0)), 921600)       # 3 + 2/8 -> 923077
        self.assertEqual(u.ftdi_baud(0xC003, 0), 960000)                    # 3 + 1/8
        # FT2232H, 120 MHz saat: 12e6/12 = 1 Mbaud, port A (wIndex dusuk bayt = 1)
        self.assertEqual(u.ftdi_baud(12, (0x02 << 8) | 1, 0x700), 1000000)

    def test_cdc_line_coding_both_usbpcap_versions(self):
        lc = struct.pack('<IBBB', 9600, 0, 2, 8)                # 9600 8E1
        lc2 = struct.pack('<IBBB', 115200, 2, 0, 7)             # 115200 7N2
        pk = descriptors(7, 0x2341, 0x0043, icls=0x0A) + ctrl_out(7, 0x21, 0x20, data=lc) + \
            ctrl_out(7, 0x21, 0x20, data=lc2, old_style=True) + ctrl_out(7, 0x21, 0x22, 3) + \
            [rec(7, 0x81, u.TR_BULK, True, b'\x00\xffbinary'), rec(7, 0x02, u.TR_BULK, False, b'cmd'),
             rec(7, 0x83, u.TR_INT, True, bytes([0xA1, 0x20, 0, 0, 0, 0, 2, 0, 0x23, 0]))]
        evs = decode(pk, address=7)
        t = infos(evs)
        self.assertIn('Uygulama port ayari: 9600 baud  8E1', t)
        self.assertIn('Uygulama port ayari: 115200 baud  7N2', t)
        self.assertIn('Hat: DTR=1  RTS=1', t)
        self.assertTrue(any('parite hatasi' in x for x in t), t)
        self.assertEqual(stream(evs, 'rx'), b'\x00\xffbinary')          # ikili veri oldugu gibi
        self.assertEqual(stream(evs, 'tx'), b'cmd')

    def test_cp210x(self):
        pk = descriptors(3, 0x10C4, 0xEA60) + ctrl_out(3, 0x41, 0x1E, data=struct.pack('<I', 921600)) + \
            ctrl_out(3, 0x41, 0x03, 0x0800) + ctrl_out(3, 0x41, 0x07, 0x0201) + \
            [rec(3, 0x81, u.TR_BULK, True, b'cp')]
        evs = decode(pk, address=3)
        self.assertIn('Uygulama port ayari: 921600 baud  8N1', infos(evs))
        self.assertIn('Hat: RTS=0', infos(evs))
        self.assertEqual(stream(evs, 'rx'), b'cp')

    def test_ch340(self):
        self.assertEqual(u.nice_baud(u.ch34x_baud(0x9887)), 115200)
        self.assertEqual(u.ch34x_lcr(0xC3), '8N1')
        self.assertEqual(u.ch34x_lcr(0xDB), '8E1')
        pk = descriptors(9, 0x1A86, 0x7523) + ctrl_out(9, 0x40, 0x9A, 0x1312, 0x9887) + \
            ctrl_out(9, 0x40, 0x9A, 0x2518, 0x00C3) + ctrl_out(9, 0x40, 0xA4, 0xFF9F) + \
            [rec(9, 0x82, u.TR_BULK, True, b'ch'), rec(9, 0x02, u.TR_BULK, False, b'340')]
        evs = decode(pk, address=9)
        self.assertIn('Uygulama port ayari: 115200 baud  8N1', infos(evs))
        self.assertIn('Hat: DTR=1  RTS=1', infos(evs))
        self.assertEqual((stream(evs, 'rx'), stream(evs, 'tx')), (b'ch', b'340'))

    def test_pl2303(self):
        pk = descriptors(2, 0x067B, 0x2303) + ctrl_out(2, 0x21, 0x20, data=struct.pack('<IBBB', 38400, 0, 0, 8))
        self.assertIn('Uygulama port ayari: 38400 baud  8N1', infos(decode(pk, address=2)))

    def test_only_target_device(self):
        pk = descriptors(4, 0x0403, 0x6001) + descriptors(6, 0x046D, 0xC52B, icls=3) + [
            rec(6, 0x81, u.TR_BULK, True, b'MOUSE'), rec(4, 0x81, u.TR_BULK, True, b'\x01\x60ok')]
        self.assertEqual(stream(decode(pk, address=4), 'rx'), b'ok')
        self.assertEqual(stream(decode(pk, vid=0x0403, pid=0x6001), 'rx'), b'ok')     # VID/PID ile kilitlen

    def test_multi_port_ftdi_tags_interface(self):
        pk = descriptors(8, 0x0403, 0x6010, bcd=0x700, n_if=2, mps=512) + [
            rec(8, 0x81, u.TR_BULK, True, b'\x01\x60A'), rec(8, 0x83, u.TR_BULK, True, b'\x01\x60B')]
        evs = decode(pk, address=8)
        self.assertEqual([(e[1], e[2]) for e in evs if e[0] == 'rx'], [(b'A', 'if0'), (b'B', 'if1')])

    def test_pcap_stream_errors_and_pcapng(self):
        with self.assertRaises(ValueError):
            u.PcapStream().feed(b'NOTAPCAPFILE' * 3)
        # pcapng (Wireshark'in kaydettigi bicim) de okunur
        shb = struct.pack('<IIIHHq', 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1) + struct.pack('<I', 28)
        idb = struct.pack('<IIHHI', 1, 20, 249, 0, 65535) + struct.pack('<I', 20)
        p = rec(1, 0x81, u.TR_BULK, True, b'x')
        pad = (4 - len(p) % 4) % 4
        epb_len = 32 + len(p) + pad
        epb = struct.pack('<IIIIIII', 6, epb_len, 0, 0, 0, len(p), len(p)) + p + b'\0' * pad + struct.pack('<I', epb_len)
        out = u.PcapStream().feed(shb + idb + epb)
        self.assertEqual(len(out), 1)
        self.assertEqual(u.parse_usbpcap(out[0][2])['data'], b'x')


# Gercek USBPcap kaydi: FTDI TTL-232R-3V3 (FT232R), uygulama portu acip 1200 -> 115200 baud, 7N1 -> 8N1,
# DTR/RTS ayarliyor. Kaynak: Wireshark issue #11743 eki "TTL-232R-3V3-USBPcap-control.pcapng".
REAL_FTDI_PCAPNG = (
    '0a0d0d0adc0000004d3c2b1a01000000ffffffffffffffff02003600496e74656c28522920436f726528544d292069352d38'
    '3335305520435055204020312e373047487a20287769746820535345342e322900000300250036342d6269742057696e646f'
    '7773203130202831363037292c206275696c6420313433393300000004004e0044756d70636170202857697265736861726b'
    '2920332e312e307263302d313032332d67636534613238313666663636202876332e312e307263302d313032332d67636534'
    '61323831366666363629000000000000dc0000000100000088000000f9000000ffff0000020035005c5c2e5c706970655c77'
    '697265736861726b5f6578746361705f5c5c2e5c55534250636170315f323031393036313330383338333100000009000100'
    '060000000c00250036342d6269742057696e646f7773203130202831363037292c206275696c642031343339330000000000'
    '0000880000000600000044000000000000002e8b0500924155c724000000240000001c000000000000000000000000000b00'
    '0001000400800208000000008006000100001200440000000600000050000000000000002e8b0500924155c72e0000002e00'
    '00001c0000000000000000000000000008000101000400800212000000011201000200000008030401600006010203010000'
    '50000000060000003c000000000000002e8b0500924155c71c0000001c0000001c0000000000000000000000000008000101'
    '000400800200000000023c0000000600000044000000000000002e8b0500924155c724000000240000001c00000000000000'
    '0000000000000b00000100040080020800000000800600020000200044000000060000005c000000000000002e8b05009241'
    '55c73c0000003c0000001c00000000000000000000000000080001010004008002200000000109022000010100802d090400'
    '0002ffffff0207058102400000070502024000005c000000060000003c000000000000002e8b0500924155c71c0000001c00'
    '00001c0000000000000000000000000008000101000400800200000000023c0000000600000044000000000000002e8b0500'
    '924155c724000000240000001c00000000000000000000000000000000010004000002080000000000090100000000004400'
    '0000060000003c000000000000002e8b0500924155c71c0000001c0000001c00000000000000000000000000000001010004'
    '00000200000000023c0000000600000044000000000000002e8b0500be7205c824000000240000001c0070ea404686a8ffff'
    '000000001700000100040000020800000000400000000000000044000000060000003c000000000000002e8b05001d7305c8'
    '1c0000001c0000001c0070ea404686a8ffff0000000008000101000400000200000000023c00000006000000440000000000'
    '00002e8b05002a7305c824000000240000001c0000e45d4586a8ffff00000000170000010004000002080000000040000000'
    '0000000044000000060000003c000000000000002e8b0500577305c81c0000001c0000001c0000e45d4586a8ffff00000000'
    '08000101000400000200000000023c0000000600000044000000000000002e8b05005e7305c824000000240000001c0010b4'
    '4c4686a8ffff000000001700000100040000020800000000400000000000000044000000060000003c000000000000002e8b'
    '0500987305c81c0000001c0000001c0010b44c4686a8ffff0000000008000101000400000200000000023c00000006000000'
    '44000000000000002e8b0500b67305c824000000240000001c0070ea404686a8ffff00000000170000010004000002080000'
    '0000400000000000000044000000060000003c000000000000002e8b0500d97305c81c0000001c0000001c0070ea404686a8'
    'ffff0000000008000101000400000200000000023c0000000600000044000000000000002e8b0500e37305c8240000002400'
    '00001c0000e45d4586a8ffff000000001700000100040000020800000000400000000000000044000000060000003c000000'
    '000000002e8b05000d7405c81c0000001c0000001c0000e45d4586a8ffff0000000008000101000400000200000000023c00'
    '00000600000044000000000000002e8b0500197405c824000000240000001c0040ebda4586a8ffff00000000170000010004'
    '0000020800000000400000000000000044000000060000003c000000000000002e8b05003e7405c81c0000001c0000001c00'
    '40ebda4586a8ffff0000000008000101000400000200000000023c000000060000003c000000000000002e8b0500477405c8'
    '1b0000001b0000001b00403b364686a8ffff000000001e00000100040000ff00000000003c000000060000003c0000000000'
    '00002e8b05005a7505c81b0000001b0000001b00403b364686a8ffff000000001e00010100040000ff00000000003c000000'
    '0600000044000000000000002e8b0500607505c824000000240000001c0060282a4586a8ffff000000001700000100040000'
    '020800000000400932000000000044000000060000003c000000000000002e8b0500807505c81c0000001c0000001c006028'
    '2a4586a8ffff0000000008000101000400000200000000023c0000000600000044000000000000002e8b0500feb105c82400'
    '0000240000001c0010b44c4686a8ffff000000001700000100040080020800000000c0050000000002004400000006000000'
    '40000000000000002e8b05005ab205c81e0000001e0000001c0010b44c4686a8ffff00000000080001010004008002020000'
    '00010160000040000000060000003c000000000000002e8b05005ab205c81c0000001c0000001c0010b44c4686a8ffff0000'
    '000008000101000400800200000000023c0000000600000044000000000000002e8b05006fb205c824000000240000001c00'
    '00e45d4586a8ffff000000001700000100040000020800000000400407000000000044000000060000003c00000000000000'
    '2e8b0500d2b205c81c0000001c0000001c0000e45d4586a8ffff0000000008000101000400000200000000023c0000000600'
    '000044000000000000002e8b05000ab305c824000000240000001c0040ebda4586a8ffff0000000017000001000400000208'
    '00000000400101010000000044000000060000003c000000000000002e8b05002fb305c81c0000001c0000001c0040ebda45'
    '86a8ffff0000000008000101000400000200000000023c0000000600000044000000000000002e8b050067b305c824000000'
    '240000001c00e0765f4586a8ffff000000001700000100040000020800000000400102020000000044000000060000003c00'
    '0000000000002e8b050099b305c81c0000001c0000001c00e0765f4586a8ffff000000000800010100040000020000000002'
    '3c0000000600000044000000000000002e8b0500c0b305c824000000240000001c00403b364686a8ffff0000000017000001'
    '00040000020800000000400200000000000044000000060000003c000000000000002e8b050016b405c81c0000001c000000'
    '1c00403b364686a8ffff0000000008000101000400000200000000023c0000000600000044000000000000002e8b05003fb4'
    '05c824000000240000001c0060282a4586a8ffff0000000017000001000400000208000000004003c4090000000044000000'
    '060000003c000000000000002e8b05008fb405c81c0000001c0000001c0060282a4586a8ffff000000000800010100040000'
    '0200000000023c0000000600000044000000000000002e8b0500f4b405c824000000240000001c00b066354586a8ffff0000'
    '0000170000010004000002080000000040031a000000000044000000060000003c000000000000002e8b050055b505c81c00'
    '00001c0000001c00b066354586a8ffff0000000008000101000400000200000000023c000000060000004400000000000000'
    '2e8b0500a6b505c824000000240000001c00405bb44586a8ffff000000001700000100040000020800000000400102020000'
    '000044000000060000003c000000000000002e8b0500d2b505c81c0000001c0000001c00405bb44586a8ffff000000000800'
    '0101000400000200000000023c0000000600000044000000000000002e8b050008b605c824000000240000001c0010b44c46'
    '86a8ffff000000001700000100040000020800000000400101010000000044000000060000003c000000000000002e8b0500'
    '51b605c81c0000001c0000001c0010b44c4686a8ffff0000000008000101000400000200000000023c000000060000004400'
    '0000000000002e8b05005fb605c824000000240000001c0000e45d4586a8ffff000000001700000100040000020800000000'
    '400408000000000044000000060000003c000000000000002e8b0500a0b605c81c0000001c0000001c0000e45d4586a8ffff'
    '0000000008000101000400000200000000023c0000000600000044000000000000002e8b0500d7b605c82400000024000000'
    '1c0010344b4686a8ffff000000001700000100040000020800000000400200000000000044000000060000003c0000000000'
    '00002e8b05007eb705c81c0000001c0000001c0010344b4686a8ffff0000000008000101000400000200000000023c000000')


class RealCapture(unittest.TestCase):
    def test_real_ftdi_usbpcap_capture(self):
        raw = bytes.fromhex(''.join(REAL_FTDI_PCAPNG))
        dec = u.UsbSerialDecoder(address=4)
        evs = []
        for _ts, link, p in u.PcapStream().feed(raw):
            self.assertEqual(link, 249)
            evs += dec.feed(u.parse_usbpcap(p))
        t = infos(evs)
        self.assertEqual(t[0], 'USB cihaz: FTDI  (VID 0403 PID 6001, adres 4)')
        self.assertIn('Hat: DTR=1  RTS=1', t)
        self.assertIn('Uygulama port ayari: 1200 baud  7N1  akis yok', t)
        self.assertEqual(t[-1], 'Uygulama port ayari: 115200 baud  8N1  akis yok')
        self.assertEqual(dec.devs[4].eps, {0x81: (64, 0), 0x02: (64, 0)})


EXTCAP_IF = 'interface {value=\\\\.\\USBPcap1}{display=USBPcap1}\ninterface {value=\\\\.\\USBPcap2}{display=USBPcap2}\n'
EXTCAP_CFG = {
    '\\\\.\\USBPcap1': ('arg {number=99}{call=--devices}{display=Attached USB Devices}{type=multicheck}\n'
                        'value {arg=99}{value=1}{display=[1] USB Root Hub (USB 3.0)}{enabled=true}\n'
                        'value {arg=99}{value=3}{display=[3] USB Input Device}{enabled=true}{parent=1}\n'),
    '\\\\.\\USBPcap2': ('value {arg=99}{value=5}{display=[5] FT232R USB UART}{enabled=true}\n'
                        'value {arg=99}{value=5_1}{display=USB Serial Converter}{enabled=false}{parent=5}\n'
                        'value {arg=99}{value=5_2}{display=USB Serial Port (COM7)}{enabled=false}{parent=5_1}\n'),
}


class Extcap(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(u.parse_extcap_interfaces(EXTCAP_IF), ['\\\\.\\USBPcap1', '\\\\.\\USBPcap2'])
        d = u.parse_extcap_devices(EXTCAP_CFG['\\\\.\\USBPcap2'])
        self.assertEqual(d[5]['name'], 'FT232R USB UART')
        self.assertIn('USB Serial Port (COM7)', d[5]['children'])
        self.assertTrue(u.com_in_name('COM7', 'USB Serial Port (COM7)'))
        self.assertFalse(u.com_in_name('COM7', 'USB Serial Port (COM17)'))


FAKE = r'''
import sys, time, struct, json
a = sys.argv[1:]
cfg = json.load(open(%(cfg)r))
if '--extcap-interfaces' in a:
    sys.stdout.write(cfg['if']); sys.exit(0)
if '--extcap-config' in a:
    sys.stdout.write(cfg['cfg'].get(a[a.index('--extcap-interface') + 1], '')); sys.exit(0)
open(%(args)r, 'w').write(json.dumps(a))
out = sys.stdout.buffer
data = bytes.fromhex(cfg['pcap'])
for i in range(0, len(data), 7):
    out.write(data[i:i + 7]); out.flush()
time.sleep(30)
'''


class EndToEnd(unittest.TestCase):
    """Sahte USBPcapCMD (python betigi) ile: port bulma, surec baslatma, ikili stdout, olaylar, kapatma."""

    def test_sniffer_with_fake_usbpcapcmd(self):
        import json
        tmp = tempfile.mkdtemp()
        pk = descriptors(5, 0x0403, 0x6001) + ctrl_out(5, 0x40, 3, 0x001a) + [
            rec(5, 0x81, u.TR_BULK, True, b'\x01\x60' + b'\r\n\x00\xffDATA'),
            rec(5, 0x02, u.TR_BULK, False, b'AT+GMR\r\n')]
        cfgp, argsp = os.path.join(tmp, 'cfg.json'), os.path.join(tmp, 'args.json')
        with open(cfgp, 'w') as f:
            json.dump({'if': EXTCAP_IF, 'cfg': EXTCAP_CFG, 'pcap': pcap(pk).hex()}, f)
        fake = os.path.join(tmp, 'fake_usbpcapcmd.py')
        with open(fake, 'w') as f:
            f.write(FAKE % {'cfg': cfgp, 'args': argsp})
        os.environ['BYSTERM_USBPCAPCMD'] = fake
        try:
            self.assertEqual(u.find_port('COM7')[:2], ('\\\\.\\USBPcap2', 5))
            self.assertIsNone(u.find_port('COM9'))
            t = u.UsbSerialSniffer('COM7')
            t.start()
            rx, tx, info, states = b'', b'', [], []
            end = time.time() + 15
            while time.time() < end and (len(rx) < 8 or not tx):
                while t.events:
                    ev = t.events.popleft()
                    if ev[0] == 'data':
                        if ev[2] == core.RX:
                            rx += ev[3]
                        else:
                            tx += ev[3]
                    elif ev[0] == 'info':
                        info.append(ev[3])
                    elif ev[0] == 'state':
                        states.append(ev[2])
                    elif ev[0] == 'closed':
                        self.fail('kapandi: ' + str(ev))
                time.sleep(0.05)
            t0 = time.time()
            t.close()
            self.assertLess(time.time() - t0, 5)
            self.assertEqual(rx, b'\r\n\x00\xffDATA')
            self.assertEqual(tx, b'AT+GMR\r\n')
            self.assertIn('open', states)
            self.assertTrue(any('115200' in x for x in info), info)
            with open(argsp) as f:
                args = json.load(f)
            self.assertEqual(args[args.index('-d') + 1], '\\\\.\\USBPcap2')
            self.assertEqual(args[args.index('--devices') + 1], '5')
            self.assertIn('--inject-descriptors', args)
            self.assertEqual(args[args.index('-o') + 1], '-')
            self.assertIsNotNone(t.proc.poll())                     # alt surec kapatildi
        finally:
            os.environ.pop('BYSTERM_USBPCAPCMD', None)

    def test_not_installed_message(self):
        os.environ['BYSTERM_USBPCAPCMD'] = ''
        old = os.environ.get('ProgramFiles')
        os.environ['ProgramFiles'] = tempfile.mkdtemp()
        try:
            if u.usbpcap_cmd() is not None:
                self.skipTest('USBPcap gercekten kurulu')
            t = u.UsbSerialSniffer('COM3')
            t.start()
            end = time.time() + 5
            closed = None
            while time.time() < end and not closed:
                while t.events:
                    ev = t.events.popleft()
                    if ev[0] == 'closed':
                        closed = ev
                time.sleep(0.05)
            self.assertIsNotNone(closed)
            self.assertIn('USBPcap', closed[2])
        finally:
            if old is None:
                os.environ.pop('ProgramFiles', None)
            else:
                os.environ['ProgramFiles'] = old


if __name__ == '__main__':
    unittest.main(verbosity=2)
