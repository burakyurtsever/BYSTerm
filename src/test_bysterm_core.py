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


import bysterm_net as net   # noqa: E402


class NetTools(unittest.TestCase):
    def test_masks_and_ranges(self):
        self.assertEqual(net.parse_mask('24'), ('255.255.255.0', 24))
        self.assertEqual(net.parse_mask('255.255.0.0'), ('255.255.0.0', 16))
        with self.assertRaises(ValueError):
            net.parse_mask('255.0.255.0')
        self.assertEqual(len(net.parse_range('192.168.1.0/24')), 254)
        self.assertEqual(net.parse_range('10.0.0.5-7'), ['10.0.0.5', '10.0.0.6', '10.0.0.7'])
        self.assertEqual(net.subnet_of('192.168.10.77', 24), '192.168.10.0/24')

    def test_plan_validation(self):
        it = net.Iface('eth0')
        with self.assertRaises(ValueError):
            net.make_plan(it, 'static', '10.0.0.5', '24', '10.9.9.1')       # gw baska alt agda
        with self.assertRaises(ValueError):
            net.make_plan(it, 'static', '300.0.0.1', '24')

    def test_helper_whitelist(self):
        real_which = net.which
        net.which = lambda n: '/usr/sbin/' + n
        try:
            for bad in (['rm', '-rf', '/'], ['ip', 'netns', 'exec', 'x', 'sh'], ['dhclient', '-sf', '/x', 'eth0'],
                        ['chmod', 'a+rw', '/etc/shadow'], ['usermod', '-aG', 'sudo', 'x'], ['nmcli', 'general', 'reload']):
                with self.assertRaises(ValueError):
                    net._helper_validate(bad)
            for good in (['ip', 'addr', 'add', '10.0.0.1/24', 'dev', 'eth0'], ['chmod', 'a+rw', '/dev/ttyUSB0'],
                         ['nmcli', 'connection', 'up', 'Wired 1'], ['dhclient', '-r', 'eth0']):
                net._helper_validate(good)
        finally:
            net.which = real_which

    def test_ping_localhost(self):
        p = net.Pinger()
        if p.backend is None:
            self.skipTest('ping yok')
        self.assertTrue(p.ping('127.0.0.1', 2000).ok)
        p.close()

    def _iperf(self, **kw):
        srv = net.IperfServer(0, bind='127.0.0.1', once=True)
        srv.start()
        c = net.IperfClient('127.0.0.1', srv.port, duration=1, **kw)
        c.start()
        self.assertTrue(wait(lambda: not c.running, 15))
        res = [e[2] for e in list(c.events) if e[0] == 'result']
        self.assertTrue(res, [e for e in c.events if e[0] == 'info'])
        self.assertGreater(res[0]['recv_bytes'], 0)
        srv.stop()

    def test_iperf_tcp(self):
        self._iperf()

    def test_iperf_tcp_reverse_parallel(self):
        self._iperf(reverse=True, parallel=3)

    def test_iperf_udp(self):
        self._iperf(udp=True, rate=20000000)


if __name__ == '__main__':
    socket.setdefaulttimeout(5)
    unittest.main(verbosity=1)
