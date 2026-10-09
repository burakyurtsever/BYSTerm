#!/usr/bin/env python3
"""BYSTerm cekirdek testleri (GUI'siz). Linux/macOS: python3 test_bysterm_core.py"""
import os
import sys
import time
import socket
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bysterm_core as c   # noqa: E402


def wait(cond, to=3.0):
    t0 = time.time()
    while time.time() - t0 < to:
        if cond():
            return True
        time.sleep(0.02)
    return False


def has(t, kind, sub=None):
    return any(e[0] == kind and (sub is None or e[2] == sub) for e in list(t.events))


class Formatter(unittest.TestCase):
    def test_hex_continuation(self):
        f = c.Formatter('hex', timestamps=False)
        out = ''.join(t for _, t in f.format(0, c.RX, bytes(range(20))) + f.format(0, c.RX, b'AB'))
        self.assertEqual(out, 'RX 00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F\n'
                              '   10 11 12 13 41 42')

    def test_ascii_crlf_split(self):
        f = c.Formatter('ascii', timestamps=False)
        out = ''.join(t for _, t in f.format(0, c.RX, b'ab\r') + f.format(0, c.RX, b'\ncd\x00'))
        self.assertEqual(out, 'RX ab\ncd<00>')

    def test_long_line_wrapped(self):
        f = c.Formatter('ascii', timestamps=False)
        out = ''.join(t for _, t in f.format(0, c.RX, b'x' * 5000))
        self.assertTrue(max(len(x) for x in out.split('\n')) <= c.Formatter.MAX_LINE + 4)

    def test_parsers(self):
        self.assertEqual(c.parse_hex('01 03 0x0A,ff'), b'\x01\x03\x0a\xff')
        self.assertEqual(c.parse_escapes(r'AT\r\n\x41'), b'AT\r\nA')
        with self.assertRaises(ValueError):
            c.parse_hex('0G')


class Network(unittest.TestCase):
    def test_tcp(self):
        s = c.TcpServerTransport('127.0.0.1', 0)
        s.start()
        self.assertTrue(wait(lambda: has(s, 'state', 'open')))
        cl = c.TcpClientTransport('127.0.0.1', s.port)
        cl.start()
        self.assertTrue(wait(lambda: s.clients))
        cl.send(b'abc')
        s.send(b'xyz')
        self.assertTrue(wait(lambda: has(cl, 'data', c.RX) and has(s, 'data', c.RX)))
        s.close()
        self.assertTrue(wait(lambda: has(cl, 'closed')))
        cl.close()

    def test_udp(self):
        u1 = c.UdpTransport('127.0.0.1', 0)
        u1.start()
        self.assertTrue(wait(lambda: has(u1, 'state', 'open')))
        u2 = c.UdpTransport('127.0.0.1', 0, '127.0.0.1', u1.local_port)
        u2.start()
        self.assertTrue(wait(lambda: has(u2, 'state', 'open')))
        u2.send(b'ping')
        self.assertTrue(wait(lambda: has(u1, 'data', c.RX)))
        u1.close()
        u2.close()


@unittest.skipUnless(c.IS_POSIX, 'pty gerekir')
class SerialPty(unittest.TestCase):
    def _pty(self):
        import tty
        m, s = os.openpty()
        tty.setraw(s)
        return m, s

    def test_bridge_forward_and_follow(self):
        import serial
        m, s = self._pty()
        link = f'/tmp/bysterm_test_{os.getpid()}'
        b = c.SerialBridge(c.SerialConfig(os.ttyname(s), 9600), link)
        b.start()
        self.assertTrue(wait(lambda: has(b, 'state', 'open')))
        app = serial.Serial(link, 115200, timeout=0.5)
        os.write(m, b'DEV')
        self.assertEqual(app.read(3), b'DEV')
        app.write(b'APP')
        time.sleep(0.2)
        self.assertEqual(os.read(m, 10), b'APP')
        self.assertTrue(wait(lambda: b.ser.baudrate == 115200))
        app.close()
        b.close()
        self.assertFalse(os.path.lexists(link))
        os.close(m)
        os.close(s)

    def test_unplug_detected(self):
        m, s = self._pty()
        t = c.SerialTransport(c.SerialConfig(os.ttyname(s), 115200))
        t.start()
        self.assertTrue(wait(lambda: has(t, 'state', 'open')))
        os.close(s)
        os.close(m)
        self.assertTrue(wait(lambda: has(t, 'closed')))
        t.close()


if __name__ == '__main__':
    socket.setdefaulttimeout(5)
    unittest.main(verbosity=1)
