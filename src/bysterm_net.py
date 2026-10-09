#!/usr/bin/env python3
"""
BYSTerm ag katmani — Qt'den BAGIMSIZ (Python 3.6+, harici kutuphane yok).

  * Ag arayuzleri : listeleme (ad, durum, hiz, IPv4/maske, gateway, DNS, DHCP, MAC) ve
                    IP/maske/gateway/DNS degistirme, DHCP'ye alma, ek IP ekleme/silme.
                    Windows: PowerShell(CIM) ile okur, netsh ile yazar (UAC).
                    Linux  : /sys + ioctl + ip + NetworkManager(nmcli); yazma pkexec ile.
                    macOS  : networksetup + ifconfig; yazma osascript (yonetici sifresi) ile.
  * Ping          : Windows IcmpSendEcho2Ex (yonetici gerekmez, dilden bagimsiz);
                    Linux/macOS yetkisiz ICMP soketi, olmazsa sistem `ping` komutu.
  * IP tarama     : alt agi paralel pingle, ARP tablosundan MAC, ters DNS.
  * iPerf3        : iperf3 PROTOKOLU (istemci + sunucu, TCP/UDP, ters yon, paralel akis).
                    Gercek `iperf3 -s` / `iperf3 -c` ile birlikte calisir.
"""
import os
import re
import sys
import json
import time
import queue
import random
import select
import socket
import shlex
import struct
import threading
import subprocess
import collections

IS_WIN = sys.platform.startswith('win')
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')

_NO_WINDOW = 0x08000000 if IS_WIN else 0   # Windows: komut calisirken konsol penceresi acma


# =========================================================================== yardimcilar
def run(cmd, timeout=20, env=None, input_text=None):
    """Komut calistir -> (donus kodu, cikti). Asla istisna firlatmaz."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                             env=env, creationflags=_NO_WINDOW)
        out, _ = p.communicate(input_text.encode() if input_text is not None else None,
                               timeout=timeout)
        enc = 'oem' if IS_WIN else 'utf-8'
        try:
            txt = out.decode(enc, errors='replace')
        except LookupError:
            txt = out.decode('utf-8', errors='replace')
        return p.returncode, txt
    except FileNotFoundError:
        return 127, f'{cmd[0]}: bulunamadi'
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:
            pass
        return 124, f'{cmd[0]}: zaman asimi'
    except OSError as e:
        return 126, str(e)


def which(name):
    for d in os.environ.get('PATH', '').split(os.pathsep) + ['/usr/sbin', '/sbin', '/usr/bin', '/bin']:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def is_ipv4(s):
    try:
        socket.inet_aton(s)
        return s.count('.') == 3
    except (OSError, TypeError):
        return False


def mask_to_prefix(mask):
    n = struct.unpack('!I', socket.inet_aton(mask))[0]
    p = bin(n).count('1')
    if n != ((0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF):
        raise ValueError(f'Gecersiz maske: {mask}')
    return p


def prefix_to_mask(p):
    p = int(p)
    return socket.inet_ntoa(struct.pack('!I', (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF if p else 0))


def ip2int(ip):
    return struct.unpack('!I', socket.inet_aton(ip))[0]


def int2ip(n):
    return socket.inet_ntoa(struct.pack('!I', n & 0xFFFFFFFF))


def parse_mask(text):
    """'255.255.255.0' veya '24' veya '/24' -> (maske, prefix)."""
    t = text.strip().lstrip('/')
    if t.isdigit():
        p = int(t)
        if not 0 <= p <= 32:
            raise ValueError('Prefix 0-32 olmali')
        return prefix_to_mask(p), p
    if not is_ipv4(t):
        raise ValueError(f'Gecersiz maske: {text}')
    return t, mask_to_prefix(t)


def parse_range(text):
    """'192.168.1.0/24' | '192.168.1.1-254' | '192.168.1.10-192.168.1.40' | tek IP -> IP listesi."""
    t = text.strip()
    if '/' in t:
        ip, p = t.split('/', 1)
        p = int(p)
        if p < 16:
            raise ValueError('En fazla /16 taranabilir')
        net = ip2int(ip) & ((0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF)
        size = 1 << (32 - p)
        if size <= 2:
            return [int2ip(net + i) for i in range(size)]
        return [int2ip(net + i) for i in range(1, size - 1)]
    if '-' in t:
        a, b = t.split('-', 1)
        a, b = a.strip(), b.strip()
        if is_ipv4(b):
            lo, hi = ip2int(a), ip2int(b)
        else:
            lo = ip2int(a)
            hi = (lo & 0xFFFFFF00) | int(b)
        if hi < lo or hi - lo > 65535:
            raise ValueError('Gecersiz aralik')
        return [int2ip(n) for n in range(lo, hi + 1)]
    if is_ipv4(t):
        return [t]
    raise ValueError(f'Gecersiz aralik: {text}')


def subnet_of(ip, prefix):
    net = ip2int(ip) & ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)
    return f'{int2ip(net)}/{prefix}'


# =========================================================================== arayuzler
class Iface:
    def __init__(self, name):
        self.name = name            # sistem adi (eth0, en0, "Ethernet 2")
        self.display = name         # gosterilecek ad
        self.desc = ''              # donanim aciklamasi
        self.mac = ''
        self.up = None              # baglanti var mi (kablo/link)
        self.speed = None           # Mbit/s
        self.kind = 'ethernet'      # ethernet | wifi | virtual | other
        self.addrs = []             # [(ip, prefix)]
        self.gateway = ''
        self.dns = []
        self.dhcp = None            # True/False/None(bilinmiyor)
        self.key = name             # uygulama icin kimlik (Windows: InterfaceIndex)
        self.nm_con = ''            # Linux NetworkManager baglanti adi
        self.mac_service = ''       # macOS ag servisi adi

    @property
    def ip(self):
        return self.addrs[0][0] if self.addrs else ''

    @property
    def prefix(self):
        return self.addrs[0][1] if self.addrs else 24

    def summary(self):
        a = ', '.join(f'{i}/{p}' for i, p in self.addrs) or 'IP yok'
        return f'{self.display}: {a}'

    def __repr__(self):
        return (f'Iface({self.display!r} up={self.up} {self.addrs} gw={self.gateway} '
                f'dhcp={self.dhcp} dns={self.dns} mac={self.mac} kind={self.kind})')


def list_interfaces(include_virtual=False):
    try:
        if IS_WIN:
            lst = _win_list()
        elif IS_MAC:
            lst = _mac_list()
        else:
            lst = _linux_list()
    except Exception as e:     # noqa: BLE001 — listeleme asla uygulamayi dusurmesin
        sys.stderr.write(f'list_interfaces: {e}\n')
        lst = []
    if not include_virtual:
        lst = [i for i in lst if i.kind != 'virtual']
    order = {'ethernet': 0, 'wifi': 1, 'other': 2, 'virtual': 3}
    lst.sort(key=lambda i: (order.get(i.kind, 9), not i.up, i.display.lower()))
    return lst


# ------------------------------------------------------------------ Linux
def _linux_list():
    import fcntl
    res = {}
    base = '/sys/class/net'
    for name in sorted(os.listdir(base)):
        if name == 'lo':
            continue
        it = Iface(name)
        p = os.path.join(base, name)

        def rd(f, default=''):
            try:
                with open(os.path.join(p, f)) as fh:
                    return fh.read().strip()
            except OSError:
                return default
        it.mac = rd('address').upper()
        oper = rd('operstate')
        it.up = oper in ('up', 'unknown') and rd('carrier', '0') == '1'
        try:
            sp = int(rd('speed', '-1'))
            it.speed = sp if sp > 0 else None
        except ValueError:
            pass
        real = os.path.realpath(p)
        if os.path.isdir(os.path.join(p, 'wireless')) or os.path.isdir(os.path.join(p, 'phy80211')):
            it.kind = 'wifi'
        elif '/virtual/' in real:
            it.kind = 'virtual'
        elif rd('type') == '1':
            it.kind = 'ethernet'
        else:
            it.kind = 'other'
        res[name] = it

    # IPv4 adresleri: `ip` varsa tum adresler (ek IP'ler dahil), yoksa ioctl (birincil adres)
    ipbin = which('ip')
    got = False
    if ipbin:
        rc, out = run([ipbin, '-o', '-4', 'addr', 'show'], timeout=5)
        if rc == 0:
            got = True
            for line in out.splitlines():
                m = re.match(r'\d+:\s+(\S+)\s+inet\s+([\d.]+)/(\d+)(.*)', line)
                if m and m.group(1).split('@')[0] in res:
                    it = res[m.group(1).split('@')[0]]
                    it.addrs.append((m.group(2), int(m.group(3))))
                    if ' dynamic' in m.group(4) and it.dhcp is None:
                        it.dhcp = True
    if not got:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for name, it in res.items():
            try:
                req = struct.pack('256s', name.encode()[:15])
                ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])     # SIOCGIFADDR
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891b, req)[20:24])   # SIOCGIFNETMASK
                it.addrs.append((ip, mask_to_prefix(mask)))
            except OSError:
                pass
        s.close()

    # gateway: /proc/net/route
    try:
        with open('/proc/net/route') as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if len(f) > 3 and f[1] == '00000000' and f[0] in res and not res[f[0]].gateway:
                    res[f[0]].gateway = socket.inet_ntoa(struct.pack('<I', int(f[2], 16)))
    except OSError:
        pass

    # NetworkManager: baglanti adi, DHCP/statik, DNS
    nm = which('nmcli')
    if nm:
        rc, out = run([nm, '-t', '-f', 'DEVICE,STATE,CONNECTION', 'device'], timeout=5)
        if rc == 0:
            for line in out.splitlines():
                parts = _nm_split(line)
                if len(parts) >= 3 and parts[0] in res and parts[2] and parts[2] != '--':
                    res[parts[0]].nm_con = parts[2]
        for it in res.values():
            if not it.nm_con:
                continue
            rc, out = run([nm, '-t', '-f', 'ipv4.method,ipv4.dns,IP4.DNS', 'connection', 'show', it.nm_con],
                          timeout=5)
            if rc != 0:
                continue
            dns = []
            for line in out.splitlines():
                k, _, v = line.partition(':')
                if k == 'ipv4.method':
                    it.dhcp = v == 'auto'
                elif k.startswith('IP4.DNS') or k == 'ipv4.dns':
                    dns += [d for d in re.split(r'[,\s]+', v) if is_ipv4(d)]
            it.dns = list(dict.fromkeys(dns))
    if not any(i.dns for i in res.values()):
        dns = _resolv_dns()
        for it in res.values():
            if it.gateway:
                it.dns = dns
    return list(res.values())


def _nm_split(line):
    # nmcli -t: ':' ayirici, '\:' kacisli
    return [p.replace('\\:', ':') for p in re.split(r'(?<!\\):', line)]


def _resolv_dns():
    out = []
    for path in ('/run/systemd/resolve/resolv.conf', '/etc/resolv.conf'):
        try:
            with open(path) as fh:
                for line in fh:
                    f = line.split()
                    if len(f) >= 2 and f[0] == 'nameserver' and is_ipv4(f[1]) and not f[1].startswith('127.'):
                        out.append(f[1])
        except OSError:
            continue
        if out:
            break
    return out


# ------------------------------------------------------------------ Windows
_WIN_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$a = @(Get-CimInstance Win32_NetworkAdapter -Filter "NetConnectionID IS NOT NULL" |
  Select-Object Index,InterfaceIndex,NetConnectionID,Name,MACAddress,NetEnabled,Speed,NetConnectionStatus,PhysicalAdapter,AdapterTypeId)
$c = @(Get-CimInstance Win32_NetworkAdapterConfiguration |
  Select-Object Index,IPAddress,IPSubnet,DefaultIPGateway,DHCPEnabled,DNSServerSearchOrder,IPEnabled)
@{a=$a;c=$c} | ConvertTo-Json -Depth 4 -Compress
"""


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _win_list():
    rc, out = run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-Command', _WIN_PS], timeout=30)
    i = out.find('{')
    if i < 0:
        raise RuntimeError(f'PowerShell cikti vermedi: {out[:200]}')
    d = json.loads(out[i:])
    cfg = {c.get('Index'): c for c in _as_list(d.get('c'))}
    res = []
    for a in _as_list(d.get('a')):
        it = Iface(a.get('NetConnectionID') or a.get('Name'))
        it.desc = a.get('Name') or ''
        it.display = it.name
        it.key = str(a.get('InterfaceIndex'))
        it.mac = (a.get('MACAddress') or '').replace('-', ':').upper()
        st = a.get('NetConnectionStatus')
        it.up = st == 2
        sp = a.get('Speed')
        try:
            sp = int(sp) // 1000000 if sp else None
            it.speed = sp if sp and sp < 1000000 else None
        except (TypeError, ValueError):
            pass
        dl = it.desc.lower()
        if any(w in dl for w in ('wi-fi', 'wireless', 'wlan', '802.11')):
            it.kind = 'wifi'
        elif not a.get('PhysicalAdapter') or any(w in dl for w in ('virtual', 'hyper-v', 'vmware',
                                                                    'virtualbox', 'loopback', 'tap-', 'vpn',
                                                                    'bluetooth', 'wan miniport')):
            it.kind = 'virtual'
        c = cfg.get(a.get('Index')) or {}
        ips = _as_list(c.get('IPAddress'))
        masks = _as_list(c.get('IPSubnet'))
        for ip, m in zip(ips, masks):
            if is_ipv4(ip) and is_ipv4(m):
                try:
                    it.addrs.append((ip, mask_to_prefix(m)))
                except ValueError:
                    pass
        gws = [g for g in _as_list(c.get('DefaultIPGateway')) if is_ipv4(g)]
        it.gateway = gws[0] if gws else ''
        it.dns = [x for x in _as_list(c.get('DNSServerSearchOrder')) if is_ipv4(x)]
        it.dhcp = c.get('DHCPEnabled')
        res.append(it)
    return res


# ------------------------------------------------------------------ macOS
def _mac_list():
    rc, out = run(['networksetup', '-listnetworkserviceorder'], timeout=10)
    services = []   # (servis, cihaz)
    cur = None
    for line in out.splitlines():
        m = re.match(r'\(\*?\d+\)\s+(.*)', line.strip())
        if m:
            cur = m.group(1).strip()
            continue
        m = re.search(r'Device:\s*([^)]+)\)', line)
        if m and cur:
            services.append((cur.lstrip('*'), m.group(1).strip()))
            cur = None
    res = []
    for svc, dev in services:
        if not dev:
            continue
        it = Iface(dev)
        it.mac_service = svc
        it.display = f'{svc} ({dev})'
        it.key = dev
        sl = svc.lower()
        it.kind = 'wifi' if ('wi-fi' in sl or 'airport' in sl) else \
            ('virtual' if any(w in sl for w in ('bridge', 'vpn', 'thunderbolt bridge')) else 'ethernet')
        rc, info = run(['networksetup', '-getinfo', svc], timeout=10)
        mask = ''
        for line in info.splitlines():
            k, _, v = line.partition(':')
            k, v = k.strip(), v.strip()
            if line.startswith('DHCP Configuration'):
                it.dhcp = True
            elif line.startswith('Manual Configuration') or line.startswith('Manually Using DHCP Router'):
                it.dhcp = False
            elif k == 'IP address' and is_ipv4(v):
                ip = v
                it.addrs.append((ip, 24))
            elif k == 'Subnet mask' and is_ipv4(v):
                mask = v
            elif k == 'Router' and is_ipv4(v):
                it.gateway = v
        if it.addrs and mask:
            it.addrs[0] = (it.addrs[0][0], mask_to_prefix(mask))
        rc, ifc = run(['ifconfig', dev], timeout=5)
        m = re.search(r'ether\s+([0-9a-f:]{17})', ifc)
        if m:
            it.mac = m.group(1).upper()
        it.up = 'status: active' in ifc
        for m in re.finditer(r'inet ([\d.]+) netmask 0x([0-9a-f]{8})', ifc):
            ip, p = m.group(1), bin(int(m.group(2), 16)).count('1')
            if all(ip != a for a, _ in it.addrs):
                it.addrs.append((ip, p))
        m = re.search(r'media:.*?(\d+)base', ifc)
        if m:
            it.speed = int(m.group(1))
        rc, dns = run(['networksetup', '-getdnsservers', svc], timeout=10)
        it.dns = [x.strip() for x in dns.splitlines() if is_ipv4(x.strip())]
        res.append(it)
    return res


# ------------------------------------------------------------------ yazma (plan + uygula)
class NetPlan:
    """Uygulanacak islem: kullaniciya GOSTERILIR, onaylanirsa yonetici yetkisiyle calisir."""

    def __init__(self, title, cmds, shell=False, note=''):
        self.title = title
        self.cmds = cmds          # [argv, ...]
        self.note = note

    def text(self):
        if IS_WIN:
            return '\n'.join(subprocess.list2cmdline(c) for c in self.cmds)
        return '\n'.join(' '.join(shlex.quote(x) for x in c) for c in self.cmds)


def make_plan(iface, mode, ip='', mask='', gw='', dns=(), action='set'):
    """mode: 'dhcp' | 'static';  action: 'set' (degistir) | 'add' (ek IP) | 'del' (IP sil)"""
    dns = [d for d in dns if d]
    for d in dns:
        if not is_ipv4(d):
            raise ValueError(f'Gecersiz DNS: {d}')
    if mode == 'static' or action in ('add', 'del'):
        if not is_ipv4(ip):
            raise ValueError(f'Gecersiz IP: {ip}')
        mask, prefix = parse_mask(mask or '24')
        if gw and not is_ipv4(gw):
            raise ValueError(f'Gecersiz gateway: {gw}')
        if gw and action == 'set' and subnet_of(gw, prefix) != subnet_of(ip, prefix):
            raise ValueError(f'Gateway ({gw}) IP ile ayni alt agda degil ({subnet_of(ip, prefix)})')
    else:
        prefix = 0
    if IS_WIN:
        return _win_plan(iface, mode, ip, mask, gw, dns, action)
    if IS_MAC:
        return _mac_plan(iface, mode, ip, mask, gw, dns, action)
    return _linux_plan(iface, mode, ip, prefix, gw, dns, action)


def _win_plan(it, mode, ip, mask, gw, dns, action):
    n = f'name={it.key}'
    base = ['netsh', 'interface', 'ipv4']
    if action == 'add':
        return NetPlan(f'{it.display}: ek IP {ip}/{mask}', [base + ['add', 'address', n, f'address={ip}', f'mask={mask}']])
    if action == 'del':
        return NetPlan(f'{it.display}: IP sil {ip}', [base + ['delete', 'address', n, f'address={ip}']])
    if mode == 'dhcp':
        cmds = [base + ['set', 'address', n, 'source=dhcp'],
                base + ['set', 'dnsservers', n, 'source=dhcp']]
        return NetPlan(f'{it.display}: DHCP', cmds)
    cmds = [base + ['set', 'address', n, 'source=static', f'address={ip}', f'mask={mask}',
                    f'gateway={gw}' if gw else 'gateway=none']]
    if dns:
        cmds.append(base + ['set', 'dnsservers', n, 'source=static', f'address={dns[0]}',
                            'register=primary', 'validate=no'])
        for i, d in enumerate(dns[1:], 2):
            cmds.append(base + ['add', 'dnsservers', n, f'address={d}', f'index={i}', 'validate=no'])
    else:
        cmds.append(base + ['set', 'dnsservers', n, 'source=static', 'address=none', 'validate=no'])
    return NetPlan(f'{it.display}: statik {ip}/{mask} gw {gw or "-"}', cmds)


def _mac_plan(it, mode, ip, mask, gw, dns, action):
    svc = it.mac_service or it.name
    if action == 'add':
        return NetPlan(f'{it.display}: ek IP {ip}', [['ifconfig', it.name, 'alias', ip, 'netmask', mask]],
                       note='macOS: ek IP gecicidir (yeniden baslatinca gider).')
    if action == 'del':
        return NetPlan(f'{it.display}: IP sil {ip}', [['ifconfig', it.name, '-alias', ip]])
    if mode == 'dhcp':
        return NetPlan(f'{it.display}: DHCP', [['networksetup', '-setdhcp', svc],
                                               ['networksetup', '-setdnsservers', svc, 'Empty']])
    cmds = [['networksetup', '-setmanual', svc, ip, mask] + ([gw] if gw else []),
            ['networksetup', '-setdnsservers', svc] + (dns or ['Empty'])]
    return NetPlan(f'{it.display}: statik {ip}/{mask} gw {gw or "-"}', cmds)


def _linux_plan(it, mode, ip, prefix, gw, dns, action):
    nm = which('nmcli')
    if nm and it.nm_con:
        c = it.nm_con
        if action == 'add':
            cmds = [[nm, 'connection', 'modify', c, '+ipv4.addresses', f'{ip}/{prefix}'],
                    [nm, 'connection', 'up', c]]
            return NetPlan(f'{it.display}: ek IP {ip}/{prefix} (NetworkManager)', cmds)
        if action == 'del':
            cmds = [[nm, 'connection', 'modify', c, '-ipv4.addresses', f'{ip}/{prefix}'],
                    [nm, 'connection', 'up', c]]
            return NetPlan(f'{it.display}: IP sil {ip} (NetworkManager)', cmds)
        if mode == 'dhcp':
            cmds = [[nm, 'connection', 'modify', c, 'ipv4.method', 'auto', 'ipv4.addresses', '',
                     'ipv4.gateway', '', 'ipv4.dns', ''],
                    [nm, 'connection', 'up', c]]
            return NetPlan(f'{it.display}: DHCP (NetworkManager)', cmds)
        cmds = [[nm, 'connection', 'modify', c, 'ipv4.method', 'manual', 'ipv4.addresses', f'{ip}/{prefix}',
                 'ipv4.gateway', gw or '', 'ipv4.dns', ' '.join(dns)],
                [nm, 'connection', 'up', c]]
        return NetPlan(f'{it.display}: statik {ip}/{prefix} gw {gw or "-"} (NetworkManager)', cmds)
    if nm and it.kind != 'virtual' and mode == 'static' and action == 'set':
        # NM var ama arayuzun baglanti profili yok (kablo takili degil vb.): yeni profil
        c = f'BYSTerm-{it.name}'
        typ = 'wifi' if it.kind == 'wifi' else 'ethernet'
        cmds = [[nm, 'connection', 'add', 'type', typ, 'ifname', it.name, 'con-name', c,
                 'ipv4.method', 'manual', 'ipv4.addresses', f'{ip}/{prefix}', 'ipv4.gateway', gw or '',
                 'ipv4.dns', ' '.join(dns)],
                [nm, 'connection', 'up', c]]
        return NetPlan(f'{it.display}: statik {ip}/{prefix} (yeni NetworkManager profili "{c}")', cmds)
    ipb = which('ip') or 'ip'
    note = 'NetworkManager yok: ayar GECICIDIR (yeniden baslatinca gider).'
    if action == 'add':
        return NetPlan(f'{it.display}: ek IP {ip}/{prefix}', [[ipb, 'addr', 'add', f'{ip}/{prefix}', 'dev', it.name]], note=note)
    if action == 'del':
        return NetPlan(f'{it.display}: IP sil {ip}', [[ipb, 'addr', 'del', f'{ip}/{prefix}', 'dev', it.name]])
    if mode == 'dhcp':
        if which('dhclient'):
            cmds = [['dhclient', '-r', it.name], ['dhclient', it.name]]
        elif which('dhcpcd'):
            cmds = [['dhcpcd', '-n', it.name]]
        elif which('udhcpc'):
            cmds = [['udhcpc', '-i', it.name, '-n', '-q']]
        else:
            raise ValueError('DHCP istemcisi bulunamadi (dhclient/dhcpcd/udhcpc)')
        return NetPlan(f'{it.display}: DHCP', cmds)
    cmds = [[ipb, 'addr', 'flush', 'dev', it.name, 'scope', 'global'],
            [ipb, 'addr', 'add', f'{ip}/{prefix}', 'dev', it.name],
            [ipb, 'link', 'set', it.name, 'up']]
    if gw:
        cmds.append([ipb, 'route', 'replace', 'default', 'via', gw, 'dev', it.name])
    return NetPlan(f'{it.display}: statik {ip}/{prefix} gw {gw or "-"}', cmds,
                   note=note + (' DNS bu modda ayarlanamaz.' if dns else ''))


def is_admin():
    if IS_WIN:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def apply_plan(plan):
    """Plani yonetici yetkisiyle calistir -> (basarili, cikti). Kullanici iptal edebilir."""
    if not plan.cmds:
        return True, ''
    h = PrivHelper.instance
    if h is not None and h.alive and not is_admin():
        return h.run(plan.cmds)       # acilista verilen yetki: sifre sormadan
    if is_admin():
        outs = []
        for c in plan.cmds:
            rc, out = run(c, timeout=60)
            outs.append(f'$ {" ".join(c)}\n{out.strip()}')
            if rc != 0:
                return False, '\n'.join(outs)
        return True, '\n'.join(outs)
    if IS_WIN:
        return _win_elevated(plan)
    if IS_MAC:
        script = ' && '.join(' '.join(shlex.quote(x) for x in c) for c in plan.cmds)
        esc = script.replace('\\', '\\\\').replace('"', '\\"')
        rc, out = run(['osascript', '-e', f'do shell script "{esc}" with administrator privileges'], timeout=120)
        if rc != 0 and '-128' in out:
            return False, 'Iptal edildi (yonetici sifresi girilmedi)'
        return rc == 0, out.strip()
    # Linux: nmcli cogu masaustunde yetkisiz calisir (polkit); olmazsa pkexec
    if plan.cmds[0][0].endswith('nmcli'):
        outs, ok = [], True
        for c in plan.cmds:
            rc, out = run(c, timeout=60)
            outs.append(out.strip())
            if rc != 0:
                ok = False
                break
        if ok:
            return True, '\n'.join(outs)
        if not re.search(r'(?i)not authori|insufficient privileges|permission|yetki', '\n'.join(outs)):
            return False, '\n'.join(outs)
    pk = which('pkexec')
    if not pk:
        return False, ('Yonetici yetkisi gerekli ve pkexec bulunamadi. BYSTerm\'i "sudo" ile calistirin '
                       'veya su komutlari elle calistirin:\n' + plan.text())
    script = ' && '.join(' '.join(shlex.quote(x) for x in c) for c in plan.cmds)
    rc, out = run([pk, '/bin/sh', '-c', script], timeout=180)
    if rc in (126, 127) and not out.strip():
        return False, 'Iptal edildi (yetki verilmedi)'
    return rc == 0, out.strip()


def _win_elevated(plan):
    """UAC ile yonetici olarak cmd calistir, ciktiyi gecici dosyadan oku."""
    import ctypes
    import tempfile
    from ctypes import wintypes
    fd, tmp = tempfile.mkstemp(suffix='.txt', prefix='bysterm_')
    os.close(fd)
    line = ' & '.join(f'({subprocess.list2cmdline(c)} || echo BYSTERM_FAIL)' for c in plan.cmds)
    params = f'/c "({line}) > "{tmp}" 2>&1"'

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR),
                    ('lpParameters', wintypes.LPCWSTR), ('lpDirectory', wintypes.LPCWSTR),
                    ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR),
                    ('hkeyClass', wintypes.HKEY), ('dwHotKey', wintypes.DWORD),
                    ('hIconOrMonitor', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]
    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040          # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = 'runas'
    sei.lpFile = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'cmd.exe')
    sei.lpParameters = params
    sei.nShow = 0                   # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.GetLastError()
        try:
            os.remove(tmp)
        except OSError:
            pass
        if err == 1223:
            return False, 'Iptal edildi (UAC onaylanmadi)'
        return False, f'Yonetici olarak calistirilamadi (hata {err})'
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 120000)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    try:
        with open(tmp, 'rb') as f:
            raw = f.read()
        os.remove(tmp)
    except OSError:
        raw = b''
    try:
        out = raw.decode('oem', errors='replace')
    except LookupError:
        out = raw.decode('utf-8', errors='replace')
    return ('BYSTERM_FAIL' not in out), out.replace('BYSTERM_FAIL', '').strip()


# =========================================================================== ping
class PingResult:
    __slots__ = ('ok', 'rtt', 'ttl', 'err')

    def __init__(self, ok, rtt=None, ttl=None, err=''):
        self.ok, self.rtt, self.ttl, self.err = ok, rtt, ttl, err


def _checksum(b):
    if len(b) % 2:
        b += b'\0'
    s = sum(struct.unpack('!%dH' % (len(b) // 2), b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


class Pinger:
    """Tek hedef icin yeniden kullanilabilir ping atici (thread basina bir tane)."""
    _win = None

    def __init__(self, source=''):
        self.source = source or ''
        self.backend = None
        self.sock = None
        self.ident = random.randint(1, 0xFFFF)
        self.seq = 0
        if IS_WIN:
            self.backend = 'win'
            self._init_win()
            return
        for kind in ('dgram', 'raw'):
            try:
                st = socket.SOCK_DGRAM if kind == 'dgram' else socket.SOCK_RAW
                s = socket.socket(socket.AF_INET, st, socket.IPPROTO_ICMP)
                if self.source:
                    s.bind((self.source, 0))
                if IS_LINUX and kind == 'dgram':
                    try:
                        s.setsockopt(socket.IPPROTO_IP, 12, 1)     # IP_RECVTTL
                    except OSError:
                        pass
                self.sock, self.backend = s, kind
                return
            except (PermissionError, OSError):
                continue
        self.backend = 'cmd' if which('ping') else None

    # ---- Windows: IcmpSendEcho2Ex (iphlpapi) — yonetici gerektirmez
    def _init_win(self):
        import ctypes
        from ctypes import wintypes
        if Pinger._win is None:
            ip = ctypes.windll.iphlpapi

            class IP_OPTION_INFORMATION(ctypes.Structure):
                _fields_ = [('Ttl', ctypes.c_ubyte), ('Tos', ctypes.c_ubyte), ('Flags', ctypes.c_ubyte),
                            ('OptionsSize', ctypes.c_ubyte), ('OptionsData', ctypes.c_void_p)]

            class ICMP_ECHO_REPLY(ctypes.Structure):
                _fields_ = [('Address', wintypes.ULONG), ('Status', wintypes.ULONG),
                            ('RoundTripTime', wintypes.ULONG), ('DataSize', wintypes.USHORT),
                            ('Reserved', wintypes.USHORT), ('Data', ctypes.c_void_p),
                            ('Options', IP_OPTION_INFORMATION)]
            ip.IcmpCreateFile.restype = wintypes.HANDLE
            ip.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
            ip.IcmpSendEcho2Ex.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                           wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                                           ctypes.POINTER(IP_OPTION_INFORMATION), ctypes.c_void_p,
                                           wintypes.DWORD, wintypes.DWORD]
            ip.IcmpSendEcho2Ex.restype = wintypes.DWORD
            Pinger._win = (ctypes, ip, IP_OPTION_INFORMATION, ICMP_ECHO_REPLY)
        ctypes, ip, _, _ = Pinger._win
        self.handle = ip.IcmpCreateFile()

    def _ping_win(self, dst, timeout_ms, size, ttl):
        ctypes, ip, OPT, REPLY = Pinger._win
        data = (b'BYSTerm' * ((size // 7) + 1))[:size]
        rsz = ctypes.sizeof(REPLY) + size + 8 + 64
        buf = ctypes.create_string_buffer(rsz)
        opt = OPT()
        opt.Ttl = ttl
        src = struct.unpack('<I', socket.inet_aton(self.source))[0] if self.source else 0
        dsti = struct.unpack('<I', socket.inet_aton(dst))[0]
        n = ip.IcmpSendEcho2Ex(self.handle, None, None, None, src, dsti, data, len(data),
                               ctypes.byref(opt), buf, rsz, int(timeout_ms))
        if n == 0:
            err = ctypes.GetLastError()
            return PingResult(False, err={11010: 'zaman asimi', 11003: 'hedef erisilemez',
                                          11050: 'genel hata'}.get(err, f'hata {err}'))
        r = REPLY.from_buffer(buf)
        if r.Status != 0:
            return PingResult(False, err={11010: 'zaman asimi', 11003: 'hedef erisilemez',
                                          11013: 'TTL asildi', 11002: 'ag erisilemez'}.get(r.Status, f'durum {r.Status}'))
        return PingResult(True, float(r.RoundTripTime), r.Options.Ttl)

    # ---- Linux/macOS soket
    def _ping_sock(self, dst, timeout_ms, size, ttl):
        s = self.sock
        self.seq = (self.seq + 1) & 0xFFFF
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
        except OSError:
            pass
        payload = struct.pack('!d', time.time()) + (b'BYSTerm' * (size // 7 + 1))[:max(0, size - 8)]
        hdr = struct.pack('!BBHHH', 8, 0, 0, self.ident, self.seq)
        pkt = hdr[:2] + struct.pack('!H', _checksum(hdr + payload)) + hdr[4:] + payload
        t0 = time.perf_counter()
        try:
            s.sendto(pkt, (dst, 0))
        except OSError as e:
            return PingResult(False, err=str(e.strerror or e))
        deadline = t0 + timeout_ms / 1000.0
        while True:
            left = deadline - time.perf_counter()
            if left <= 0:
                return PingResult(False, err='zaman asimi')
            r, _, _ = select.select([s], [], [], left)
            if not r:
                return PingResult(False, err='zaman asimi')
            try:
                if IS_LINUX and self.backend == 'dgram':
                    data, anc, _, addr = s.recvmsg(65535, 64)
                else:
                    data, addr = s.recvfrom(65535)
                    anc = []
            except OSError:
                continue
            rtt = (time.perf_counter() - t0) * 1000.0
            rttl = None
            for lvl, typ, val in anc:
                if lvl == socket.IPPROTO_IP and typ == 2 and len(val) >= 1:   # IP_TTL
                    rttl = val[0]
            if data and (data[0] >> 4) == 4 and len(data) >= 28:           # IP basligi var (raw / macOS)
                ihl = (data[0] & 0x0F) * 4
                rttl = data[8]
                data = data[ihl:]
            if len(data) < 8 or addr[0] != dst:
                continue
            typ, code, _, rid, rseq = struct.unpack('!BBHHH', data[:8])
            if typ == 0 and rseq == self.seq and (self.backend == 'dgram' or rid == self.ident):
                return PingResult(True, rtt, rttl)
            if typ == 3:
                return PingResult(False, err='hedef erisilemez')
            if typ == 11:
                return PingResult(False, err='TTL asildi')

    def _ping_cmd(self, dst, timeout_ms, size, ttl):
        env = dict(os.environ, LC_ALL='C', LANG='C')
        if IS_MAC:
            cmd = ['ping', '-n', '-c', '1', '-W', str(int(timeout_ms)), '-s', str(size), '-m', str(ttl)]
        else:
            cmd = ['ping', '-n', '-c', '1', '-W', str(max(1, int(round(timeout_ms / 1000.0)))),
                   '-s', str(size), '-t', str(ttl)]
        if self.source:
            cmd += ['-S' if IS_MAC else '-I', self.source]
        rc, out = run(cmd + [dst], timeout=timeout_ms / 1000.0 + 3, env=env)
        m = re.search(r'time[=<]([\d.]+)\s*ms', out)
        if rc == 0 and m:
            t = re.search(r'ttl=(\d+)', out, re.I)
            return PingResult(True, float(m.group(1)), int(t.group(1)) if t else None)
        if 'nreachable' in out:
            return PingResult(False, err='hedef erisilemez')
        return PingResult(False, err='zaman asimi')

    def ping(self, dst, timeout_ms=1000, size=32, ttl=64):
        if self.backend == 'win':
            return self._ping_win(dst, timeout_ms, size, ttl)
        if self.backend in ('dgram', 'raw'):
            return self._ping_sock(dst, timeout_ms, size, ttl)
        if self.backend == 'cmd':
            return self._ping_cmd(dst, timeout_ms, size, ttl)
        return PingResult(False, err='ping desteklenmiyor')

    def close(self):
        if self.sock:
            self.sock.close()
        if self.backend == 'win' and getattr(self, 'handle', None):
            Pinger._win[1].IcmpCloseHandle(self.handle)
            self.handle = None


def resolve(host):
    if is_ipv4(host):
        return host
    return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]


class PingWorker:
    """Surekli ping (bir hedef). Olaylar deque'ya: ('reply', ts, seq, PingResult) / ('info', ts, text)."""

    def __init__(self, host, interval=1.0, timeout_ms=1000, size=32, count=0, source='', ttl=64):
        self.host, self.interval, self.timeout_ms = host, interval, timeout_ms
        self.size, self.count, self.source, self.ttl = size, count, source, ttl
        self.events = collections.deque()
        self._stop = threading.Event()
        self.ip = ''
        self.thread = threading.Thread(target=self._run, daemon=True, name=f'ping-{host}')

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    @property
    def running(self):
        return self.thread.is_alive()

    def _run(self):
        try:
            self.ip = resolve(self.host)
        except OSError as e:
            self.events.append(('info', time.time(), f'{self.host}: cozulemedi ({e})'))
            self.events.append(('end', time.time()))
            return
        p = Pinger(self.source)
        if p.backend is None:
            self.events.append(('info', time.time(), 'Bu sistemde ping atilamiyor'))
            self.events.append(('end', time.time()))
            return
        seq = 0
        try:
            while not self._stop.is_set():
                seq += 1
                t0 = time.monotonic()
                r = p.ping(self.ip, self.timeout_ms, self.size, self.ttl)
                self.events.append(('reply', time.time(), seq, r))
                if self.count and seq >= self.count:
                    break
                self._stop.wait(max(0.0, self.interval - (time.monotonic() - t0)))
        finally:
            p.close()
            self.events.append(('end', time.time()))


# =========================================================================== IP tarama
def arp_table():
    """IP -> MAC (sistem ARP onbellegi)."""
    res = {}
    try:
        if IS_LINUX and os.path.exists('/proc/net/arp'):
            with open('/proc/net/arp') as f:
                for line in f.readlines()[1:]:
                    p = line.split()
                    if len(p) >= 4 and p[3] != '00:00:00:00:00:00':
                        res[p[0]] = p[3].upper()
            return res
        rc, out = run(['arp', '-a'] if IS_WIN else ['arp', '-an'], timeout=10)
        for m in re.finditer(r'(\d+\.\d+\.\d+\.\d+)\D+?([0-9a-fA-F]{1,2}(?:[-:][0-9a-fA-F]{1,2}){5})', out):
            mac = ':'.join(x.zfill(2) for x in re.split('[-:]', m.group(2))).upper()
            if mac not in ('FF:FF:FF:FF:FF:FF', '00:00:00:00:00:00'):
                res[m.group(1)] = mac
    except Exception:
        pass
    return res


class Scanner:
    """Paralel ping taramasi. Olaylar: ('host', ip, rtt, ttl) / ('progress', done, total) /
    ('name', ip, hostname) / ('mac', ip, mac) / ('end', found, sure)."""

    def __init__(self, ips, timeout_ms=600, workers=64, source='', resolve_names=True):
        self.ips = list(ips)
        self.timeout_ms = timeout_ms
        self.workers = max(1, min(workers, len(self.ips)))
        self.source = source
        self.resolve_names = resolve_names
        self.events = collections.deque()
        self._stop = threading.Event()
        self._q = queue.Queue()
        self.found = []

    def start(self):
        for ip in self.ips:
            self._q.put(ip)
        threading.Thread(target=self._run, daemon=True, name='scan').start()

    def stop(self):
        self._stop.set()

    def _run(self):
        t0 = time.monotonic()
        done = [0]
        lock = threading.Lock()

        def worker():
            p = Pinger(self.source)
            try:
                while not self._stop.is_set():
                    try:
                        ip = self._q.get_nowait()
                    except queue.Empty:
                        return
                    r = p.ping(ip, self.timeout_ms, 32)
                    if not r.ok:          # ikinci sans (ilk ARP cozumlemesi gecikebilir)
                        r = p.ping(ip, self.timeout_ms, 32)
                    with lock:
                        done[0] += 1
                        if r.ok:
                            self.found.append(ip)
                            self.events.append(('host', ip, r.rtt, r.ttl))
                        self.events.append(('progress', done[0], len(self.ips)))
            finally:
                p.close()
        ths = [threading.Thread(target=worker, daemon=True) for _ in range(self.workers)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        arp = arp_table()
        for ip in self.found:
            if ip in arp:
                self.events.append(('mac', ip, arp[ip]))
        # ARP'de olup pinge cevap vermeyen (firewall'lu) cihazlar da var olabilir
        scanned = set(self.ips)
        for ip, mac in arp.items():
            if ip in scanned and ip not in self.found and not self._stop.is_set():
                self.found.append(ip)
                self.events.append(('host', ip, None, None))
                self.events.append(('mac', ip, mac))
        if self.resolve_names and not self._stop.is_set():
            names_q = queue.Queue()
            for ip in self.found:
                names_q.put(ip)

            def namer():
                while not self._stop.is_set():
                    try:
                        ip = names_q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        n = socket.gethostbyaddr(ip)[0]
                        if n and n != ip:
                            self.events.append(('name', ip, n))
                    except (OSError, UnicodeError):
                        pass
            nts = [threading.Thread(target=namer, daemon=True) for _ in range(min(16, len(self.found) or 1))]
            for t in nts:
                t.start()
            for t in nts:
                t.join(timeout=8)
        self.events.append(('end', len(self.found), time.monotonic() - t0))


def suggest_free_ip(device_ip, prefix=24, source='', exclude=()):
    """Cihazin alt aginda bos bir IP oner (.250 -> .200 arasi, cevap vermeyen ilk adres)."""
    net = ip2int(device_ip) & ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)
    bcast = net | (~((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF) & 0xFFFFFFFF)
    cands = []
    for off in list(range(250, 199, -1)) + list(range(199, 1, -1)):
        n = net + off
        if n >= bcast or n <= net:
            continue
        ip = int2ip(n)
        if ip != device_ip and ip not in exclude:
            cands.append(ip)
        if len(cands) >= 24:
            break
    arp = arp_table()
    cands = [c for c in cands if c not in arp]
    busy = set()
    lock = threading.Lock()

    def chk(ip):
        p = Pinger(source)
        try:
            if p.ping(ip, 400).ok:
                with lock:
                    busy.add(ip)
        finally:
            p.close()
    ths = [threading.Thread(target=chk, args=(c,), daemon=True) for c in cands[:12]]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=3)
    for c in cands:
        if c not in busy:
            return c
    return None


# =========================================================================== iPerf3 protokolu
TEST_START, TEST_RUNNING, TEST_END = 1, 2, 4
PARAM_EXCHANGE, CREATE_STREAMS, SERVER_TERMINATE, CLIENT_TERMINATE = 9, 10, 11, 12
EXCHANGE_RESULTS, DISPLAY_RESULTS, IPERF_START, IPERF_DONE = 13, 14, 15, 16
ACCESS_DENIED, SERVER_ERROR = -1, -2
COOKIE_SIZE = 37
UDP_CONNECT_MSG = 0x36373839
UDP_CONNECT_REPLY = 0x39383736
LEGACY_UDP_CONNECT_REPLY = 987654321
IPERF_VERSION = 'BYSTerm-iperf3-1.0'


def _make_cookie():
    chars = 'abcdefghijklmnopqrstuvwxyz234567'
    return (''.join(random.choice(chars) for _ in range(COOKIE_SIZE - 1)) + '\0').encode('ascii')


def _recvn(sock, n, stop=None):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            if stop is not None and stop.is_set():
                raise ConnectionAbortedError('durduruldu')
            continue
        if not chunk:
            raise ConnectionError('baglanti kapandi')
        buf += chunk
    return bytes(buf)


def _send_state(sock, st):
    sock.sendall(struct.pack('b', st))


def _recv_state(sock, stop=None):
    return struct.unpack('b', _recvn(sock, 1, stop))[0]


def _send_json(sock, obj):
    b = json.dumps(obj).encode()
    sock.sendall(struct.pack('!I', len(b)) + b)


def _recv_json(sock, stop=None):
    n = struct.unpack('!I', _recvn(sock, 4, stop))[0]
    if n > 10 * 1024 * 1024:
        raise ValueError('JSON cok buyuk')
    return json.loads(_recvn(sock, n, stop).decode('utf-8', errors='replace'))


def fmt_rate(bps):
    for unit, div in (('Gbit/s', 1e9), ('Mbit/s', 1e6), ('kbit/s', 1e3)):
        if bps >= div:
            return f'{bps / div:.2f} {unit}'
    return f'{bps:.0f} bit/s'


def fmt_bytes(n):
    for unit, div in (('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if n >= div:
            return f'{n / div:.2f} {unit}'
    return f'{n} B'


def _stream_ids(n):
    # iperf3 akis kimlikleri: 1, 3, 4, 5, ... (iperf_add_stream'deki tuhaflik)
    return [1] + list(range(3, n + 2))


class _Stream:
    def __init__(self, sid, sock=None, addr=None):
        self.id = sid
        self.sock = sock
        self.addr = addr          # UDP sunucu: istemci adresi
        self.bytes = 0
        self.packets = 0
        self.errors = 0           # UDP: kayip
        self.ooo = 0
        self.jitter = 0.0
        self._prev_transit = None
        self._next_seq = 1
        self.pmax = 0

    def udp_rx(self, data, now):
        if len(data) < 12:
            return
        sec, usec, seq = struct.unpack('!III', data[:12])
        self.bytes += len(data)
        transit = now - (sec + usec / 1e6)
        if self._prev_transit is not None:
            d = abs(transit - self._prev_transit)
            self.jitter += (d - self.jitter) / 16.0
        self._prev_transit = transit
        if seq >= self._next_seq:
            if seq > self._next_seq:
                self.errors += seq - self._next_seq
            self._next_seq = seq + 1
        else:
            self.ooo += 1
            if self.errors > 0:
                self.errors -= 1
        self.pmax = max(self.pmax, seq)
        self.packets = self.pmax


class _IperfBase:
    """Ortak: olay kuyrugu, veri gonderme/alma dongüleri, raporlama."""

    def __init__(self):
        self.events = collections.deque()
        self._stop = threading.Event()
        self._done = threading.Event()
        self.streams = []
        self.udp = False
        self.blksize = 128 * 1024
        self.rate = 0
        self.duration = 10
        self.sending = True
        self._t_start = None

    def _info(self, text, level='info'):
        self.events.append(('info', time.time(), level, text))

    def stop(self):
        self._stop.set()

    @property
    def running(self):
        return not self._done.is_set()

    # --- veri donguleri
    def _tcp_send(self, st, run_ev):
        buf = memoryview(os.urandom(min(self.blksize, 1 << 20)))
        s = st.sock
        s.settimeout(1.0)
        rate = self.rate / max(1, len(self.streams)) if self.rate else 0
        t0 = time.monotonic()
        while run_ev.is_set():
            if rate and st.bytes * 8 > rate * (time.monotonic() - t0):
                time.sleep(0.001)
                continue
            try:
                n = s.send(buf)
            except socket.timeout:
                continue
            except OSError:
                break
            st.bytes += n

    def _tcp_recv(self, st, run_ev):
        s = st.sock
        s.settimeout(0.5)
        buf = bytearray(1 << 18)
        while not self._stop.is_set():
            try:
                n = s.recv_into(buf)
            except socket.timeout:
                if not run_ev.is_set():
                    break
                continue
            except OSError:
                break
            if not n:
                break
            st.bytes += n

    def _udp_send(self, st, run_ev, sock, addr=None):
        size = max(16, self.blksize)
        pad = os.urandom(size - 12)
        rate = (self.rate or 1000000) / max(1, len(self.streams))
        t0 = time.monotonic()
        seq = 0
        while run_ev.is_set():
            elapsed = time.monotonic() - t0
            if st.bytes * 8 > rate * elapsed:
                time.sleep(min(0.001, max(0.0, (st.bytes * 8 / rate) - elapsed)))
                continue
            seq += 1
            now = time.time()
            pkt = struct.pack('!III', int(now), int((now % 1) * 1e6), seq) + pad
            try:
                if addr:
                    sock.sendto(pkt, addr)
                else:
                    sock.send(pkt)
            except (BlockingIOError, socket.timeout):
                seq -= 1
                time.sleep(0.0005)
                continue
            except OSError:
                if not run_ev.is_set():
                    break
                time.sleep(0.001)
                seq -= 1
                continue
            st.bytes += len(pkt)
            st.packets = seq

    def _udp_recv_connected(self, st, run_ev):
        s = st.sock
        s.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data = s.recv(65536)
            except socket.timeout:
                if not run_ev.is_set():
                    break
                continue
            except OSError:
                if not run_ev.is_set():
                    break
                continue
            st.udp_rx(data, time.time())

    # --- raporlama
    def _reporter(self, run_ev, interval=1.0):
        t0 = self._t_start
        last_t, last_b = t0, 0
        while run_ev.is_set() and not self._stop.is_set():
            time.sleep(0.05)
            now = time.monotonic()
            if now - last_t >= interval:
                tot = sum(s.bytes for s in self.streams)
                dt = now - last_t
                extra = {}
                if self.udp and not self.sending:
                    extra = {'jitter_ms': max(s.jitter for s in self.streams) * 1000,
                             'lost': sum(s.errors for s in self.streams),
                             'packets': sum(s.packets for s in self.streams)}
                self.events.append(('interval', last_t - t0, now - t0, tot - last_b,
                                    (tot - last_b) * 8 / dt, extra))
                last_t, last_b = now, tot

    def _my_results(self, elapsed):
        return {
            'cpu_util_total': 0.0, 'cpu_util_user': 0.0, 'cpu_util_system': 0.0,
            'sender_has_retransmits': 0,
            'streams': [{'id': s.id, 'bytes': s.bytes, 'retransmits': -1,
                         'jitter': s.jitter if not self.sending else 0.0,
                         'errors': s.errors if not self.sending else 0,
                         'omitted_errors': 0,
                         'packets': s.packets, 'omitted_packets': 0,
                         'start_time': 0.0, 'end_time': elapsed} for s in self.streams],
        }

    def _summary(self, mine, theirs, elapsed):
        """Gonderen/alan ozet satirlari olaya cevir."""
        def tot(res):
            return sum(int(s.get('bytes', 0)) for s in (res or {}).get('streams', []))
        snd, rcv = (mine, theirs) if self.sending else (theirs, mine)
        sb, rb = tot(snd), tot(rcv)
        res = {'elapsed': elapsed, 'sent_bytes': sb, 'recv_bytes': rb,
               'sent_bps': sb * 8 / elapsed if elapsed else 0, 'recv_bps': rb * 8 / elapsed if elapsed else 0,
               'udp': self.udp}
        if self.udp and rcv:
            st = rcv.get('streams', [])
            res['jitter_ms'] = max([float(s.get('jitter', 0)) for s in st] or [0]) * 1000
            res['lost'] = sum(int(s.get('errors', 0)) for s in st)
            res['packets'] = sum(int(s.get('packets', 0)) for s in (snd or {}).get('streams', [])) or \
                sum(int(s.get('packets', 0)) for s in st)
        self.events.append(('result', time.time(), res))
        return res


class IperfClient(_IperfBase):
    def __init__(self, host, port=5201, udp=False, duration=10, parallel=1, reverse=False,
                 rate=0, blksize=0, bind=''):
        super().__init__()
        self.host, self.port = host, int(port)
        self.udp = udp
        self.duration = max(1, int(duration))
        self.parallel = max(1, min(64, int(parallel)))
        self.reverse = reverse
        self.sending = not reverse
        self.rate = int(rate) if rate else (1000000 if udp else 0)
        self.blksize = int(blksize) if blksize else (1460 if udp else 128 * 1024)
        self.bind = bind or ''
        self.ctrl = None

    def start(self):
        threading.Thread(target=self._guard, daemon=True, name='iperf-client').start()

    def _guard(self):
        try:
            self._run()
        except Exception as e:   # noqa: BLE001
            if not self._stop.is_set():
                self._info(f'Hata: {_ierr(e)}', 'error')
            else:
                self._info('Durduruldu', 'warn')
        finally:
            for s in self.streams:
                try:
                    s.sock.close()
                except Exception:
                    pass
            if self.ctrl:
                try:
                    self.ctrl.close()
                except Exception:
                    pass
            self._done.set()
            self.events.append(('end', time.time()))

    def _connect(self, typ):
        src = (self.bind, 0) if self.bind else None
        if typ == socket.SOCK_STREAM:
            s = socket.create_connection((self.host, self.port), timeout=5, source_address=src)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s
        ai = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_DGRAM)[0]
        s = socket.socket(ai[0], socket.SOCK_DGRAM)
        if src:
            s.bind(src)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        except OSError:
            pass
        s.connect(ai[4])
        return s

    def _run(self):
        proto = 'UDP' if self.udp else 'TCP'
        self._info(f'{self.host}:{self.port} iperf3 sunucusuna baglaniliyor ({proto}, {self.duration} sn, '
                   f'{self.parallel} akis{", TERS yon (sunucu gonderir)" if self.reverse else ""})')
        self.ctrl = c = self._connect(socket.SOCK_STREAM)
        c.settimeout(1.0)
        cookie = _make_cookie()
        c.sendall(cookie)
        run_ev = threading.Event()
        threads = []
        results_mine = results_theirs = None
        elapsed = 0.0
        while True:
            st = _recv_state(c, self._stop)
            if st == PARAM_EXCHANGE:
                p = {'tcp' if not self.udp else 'udp': True, 'omit': 0, 'time': self.duration,
                     'num': 0, 'blockcount': 0, 'parallel': self.parallel, 'len': self.blksize,
                     'pacing_timer': 1000, 'client_version': IPERF_VERSION}
                if self.reverse:
                    p['reverse'] = True
                if self.rate:
                    p['bandwidth'] = self.rate
                _send_json(c, p)
            elif st == CREATE_STREAMS:
                for sid in _stream_ids(self.parallel):
                    if self.udp:
                        s = self._connect(socket.SOCK_DGRAM)
                        s.settimeout(3)
                        s.send(struct.pack('<I', UDP_CONNECT_MSG))
                        try:
                            s.recv(64)
                        except socket.timeout:
                            raise ConnectionError('UDP akisi kurulamadi (sunucu cevap vermedi / firewall?)')
                    else:
                        s = self._connect(socket.SOCK_STREAM)
                        s.sendall(cookie)
                    self.streams.append(_Stream(sid, s))
            elif st == TEST_START:
                pass
            elif st == TEST_RUNNING:
                run_ev.set()
                self._t_start = time.monotonic()
                for s in self.streams:
                    if self.udp:
                        fn = (lambda st_=s: self._udp_send(st_, run_ev, st_.sock)) if self.sending \
                            else (lambda st_=s: self._udp_recv_connected(st_, run_ev))
                    else:
                        fn = (lambda st_=s: self._tcp_send(st_, run_ev)) if self.sending \
                            else (lambda st_=s: self._tcp_recv(st_, run_ev))
                    t = threading.Thread(target=fn, daemon=True)
                    t.start()
                    threads.append(t)
                rep = threading.Thread(target=self._reporter, args=(run_ev,), daemon=True)
                rep.start()
                end = self._t_start + self.duration
                while time.monotonic() < end and not self._stop.is_set():
                    time.sleep(0.05)
                elapsed = time.monotonic() - self._t_start
                if self.sending:
                    run_ev.clear()
                    for t in threads:
                        t.join(timeout=2)
                _send_state(c, TEST_END)
                if not self.sending:
                    run_ev.clear()
                rep.join(timeout=1)
                if self._stop.is_set():
                    self._info('Kullanici durdurdu; sonuclar aliniyor...', 'warn')
            elif st == EXCHANGE_RESULTS:
                for t in threads:
                    t.join(timeout=1.5)
                results_mine = self._my_results(elapsed)
                _send_json(c, results_mine)
                results_theirs = _recv_json(c)
            elif st == DISPLAY_RESULTS:
                _send_state(c, IPERF_DONE)
                self._summary(results_mine, results_theirs, elapsed or self.duration)
                return
            elif st == ACCESS_DENIED:
                raise ConnectionError('Sunucu mesgul (baska bir test calisiyor)')
            elif st == SERVER_ERROR:
                try:
                    ie, en = struct.unpack('!ii', _recvn(c, 8))
                except Exception:
                    ie, en = 0, 0
                raise ConnectionError(f'Sunucu hatasi (iperf hata {ie}, errno {en})')
            elif st in (SERVER_TERMINATE, CLIENT_TERMINATE):
                raise ConnectionError('Sunucu testi sonlandirdi')
            elif st == IPERF_DONE:
                return


class IperfServer(_IperfBase):
    """iperf3 sunucusu: gercek `iperf3 -c` istemcileri ve BYSTerm istemcileri ile calisir."""

    def __init__(self, port=5201, bind='', once=False):
        super().__init__()
        self.port = int(port)
        self.bind = bind or ''
        self.once = once
        self.lsock = None
        self.busy = False

    def start(self):
        fam = socket.AF_INET
        ls = socket.socket(fam, socket.SOCK_STREAM)
        if not IS_WIN:
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((self.bind or '0.0.0.0', self.port))
        ls.listen(16)
        ls.settimeout(0.3)
        self.lsock = ls
        self.port = ls.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True, name='iperf-server').start()

    def _loop(self):
        self._info(f'iperf3 sunucusu dinliyor: {self.bind or "0.0.0.0"}:{self.port}  '
                   f'(diger cihazdan: iperf3 -c <bu PC IP> -p {self.port})')
        try:
            while not self._stop.is_set():
                try:
                    c, addr = self.lsock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    self._session(c, addr)
                except Exception as e:     # noqa: BLE001
                    if not self._stop.is_set():
                        self._info(f'{addr[0]}: test hatasi: {_ierr(e)}', 'error')
                finally:
                    for s in self.streams:
                        try:
                            if s.sock and not self.udp:
                                s.sock.close()
                        except Exception:
                            pass
                    try:
                        c.close()
                    except Exception:
                        pass
                    self.busy = False
                if self.once:
                    break
        finally:
            try:
                self.lsock.close()
            except Exception:
                pass
            self._done.set()
            self.events.append(('end', time.time()))

    def _accept_stream(self, cookie, timeout=10.0):
        """Yeni veri baglantisi bekle; arada gelen yeni KONTROL baglantilarini reddet."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                s, addr = self.lsock.accept()
            except socket.timeout:
                continue
            s.settimeout(5)
            try:
                ck = _recvn(s, COOKIE_SIZE)
            except Exception:
                s.close()
                continue
            if ck == cookie:
                return s
            try:     # baska bir istemci: mesgul
                _send_state(s, ACCESS_DENIED)
            except OSError:
                pass
            s.close()
        raise ConnectionError('veri akisi baglanmadi (zaman asimi)')

    def _session(self, c, addr):
        self.busy = True
        self.streams = []
        c.settimeout(5)
        cookie = _recvn(c, COOKIE_SIZE)
        c.settimeout(1.0)
        _send_state(c, PARAM_EXCHANGE)
        p = _recv_json(c, self._stop)
        self.udp = bool(p.get('udp'))
        self.duration = int(p.get('time') or 10)
        n = max(1, int(p.get('parallel') or 1))
        reverse = bool(p.get('reverse'))
        self.sending = reverse
        self.blksize = int(p.get('len') or (1460 if self.udp else 128 * 1024))
        self.rate = int(p.get('bandwidth') or (1000000 if self.udp else 0))
        if p.get('bidirectional'):
            _send_state(c, SERVER_ERROR)
            c.sendall(struct.pack('!ii', 0, 0))
            raise ValueError('Cift yonlu (--bidir) test desteklenmiyor')
        ver = p.get('client_version', '?')
        self._info(f'{addr[0]} baglandi ({ver}): {"UDP" if self.udp else "TCP"}, {self.duration} sn, '
                   f'{n} akis, {"sunucu GONDERIR (-R)" if reverse else "sunucu ALIR"}')
        usock = None
        if self.udp:
            usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if not IS_WIN:
                usock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                usock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
                usock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            except OSError:
                pass
            usock.bind((self.bind or '0.0.0.0', self.port))
            usock.settimeout(10)
        _send_state(c, CREATE_STREAMS)
        try:
            for sid in _stream_ids(n):
                if self.udp:
                    data, uaddr = usock.recvfrom(64)
                    usock.sendto(struct.pack('<I', UDP_CONNECT_REPLY), uaddr)
                    self.streams.append(_Stream(sid, usock, uaddr))
                else:
                    s = self._accept_stream(cookie)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.streams.append(_Stream(sid, s))
            _send_state(c, TEST_START)
            _send_state(c, TEST_RUNNING)
            run_ev = threading.Event()
            run_ev.set()
            self._t_start = time.monotonic()
            threads = []
            if self.udp and not reverse:
                byaddr = {s.addr: s for s in self.streams}

                def udp_rx():
                    usock.settimeout(0.3)
                    while run_ev.is_set() and not self._stop.is_set():
                        try:
                            data, a = usock.recvfrom(65536)
                        except socket.timeout:
                            continue
                        except OSError:
                            continue
                        st = byaddr.get(a)
                        if st:
                            st.udp_rx(data, time.time())
                threads.append(threading.Thread(target=udp_rx, daemon=True))
            else:
                for s in self.streams:
                    if self.udp:
                        fn = (lambda st_=s: self._udp_send(st_, run_ev, usock, st_.addr))
                    elif reverse:
                        fn = (lambda st_=s: self._tcp_send(st_, run_ev))
                    else:
                        fn = (lambda st_=s: self._tcp_recv(st_, run_ev))
                    threads.append(threading.Thread(target=fn, daemon=True))
            for t in threads:
                t.start()
            rep = threading.Thread(target=self._reporter, args=(run_ev,), daemon=True)
            rep.start()
            # istemci TEST_END gonderene kadar
            limit = time.monotonic() + self.duration + 30
            st = None
            while time.monotonic() < limit and not self._stop.is_set():
                try:
                    st = _recv_state(c)
                    break
                except socket.timeout:
                    continue
            elapsed = time.monotonic() - self._t_start
            run_ev.clear()
            for t in threads:
                t.join(timeout=2)
            rep.join(timeout=1)
            if st != TEST_END:
                raise ConnectionError(f'beklenmeyen durum: {st}')
            _send_state(c, EXCHANGE_RESULTS)
            theirs = _recv_json(c, self._stop)
            mine = self._my_results(elapsed)
            _send_json(c, mine)
            _send_state(c, DISPLAY_RESULTS)
            try:
                c.settimeout(3)
                _recv_state(c)
            except Exception:
                pass
            self._summary(mine, theirs, elapsed)
        finally:
            if usock:
                usock.close()

    def stop(self):
        self._stop.set()


def _ierr(e):
    if isinstance(e, ConnectionRefusedError):
        return 'Baglanti reddedildi (karsida iperf3 sunucusu calismiyor mu? iperf3 -s)'
    if isinstance(e, socket.timeout):
        return 'Zaman asimi (adres/port/firewall?)'
    return f'{type(e).__name__}: {e}' if not isinstance(e, ConnectionError) else str(e)


# =========================================================================== yonetici yardimcisi
# Uygulama acilisinda BIR KEZ yetki istenir; sonra ag/seri izin islemleri sifre sormadan yapilir.
#   Windows     : uygulamanin kendisi yonetici olarak yeniden baslatilir (UAC).
#   Linux       : pkexec ile kucuk bir yardimci surec (bu program --net-helper) root calisir,
#                 stdin/stdout borusundan SADECE izin listesindeki komutlari calistirir.
#   macOS       : ayni yardimci, osascript (yonetici sifresi) ile; iletisim adli borular (FIFO).
_HELPER_TOOLS = {'nmcli', 'ip', 'dhclient', 'dhcpcd', 'udhcpc', 'networksetup', 'ifconfig',
                 'usermod', 'dseditgroup', 'chmod'}
_DEV_RE = re.compile(r'^/dev/(tty|cu)[A-Za-z0-9._-]+$')
_USER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9._-]{0,31}$')


def _helper_validate(argv):
    """Yardimci sadece bunlari calistirir; argv[0] her zaman sistemdeki gercek yola cevrilir."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError('gecersiz komut')
    base = os.path.basename(argv[0])
    if base not in _HELPER_TOOLS:
        raise ValueError(f'izin verilmeyen komut: {base}')
    if base == 'chmod' and not (len(argv) == 3 and argv[1] == 'a+rw' and _DEV_RE.match(argv[2])):
        raise ValueError('chmod sadece seri port cihazlari icin')
    if base == 'usermod' and not (len(argv) == 4 and argv[1] == '-aG' and argv[2] in ('dialout', 'uucp')
                                  and _USER_RE.match(argv[3])):
        raise ValueError('usermod sadece dialout/uucp grubuna ekleme icin')
    if base == 'dseditgroup':
        raise ValueError('desteklenmiyor')
    args = argv[1:]
    iface_ok = re.compile(r'^[A-Za-z0-9._:@-]{1,32}$')
    if base == 'ip':
        # sadece addr/route/link; 'exec' (netns/vrf exec = keyfi komut) ve batch dosyasi yasak
        if not args or args[0] not in ('addr', 'address', 'route', 'link', '-o', '-4') or \
                any(a in ('exec', 'netns', 'vrf', '-b', '-batch', '-force') for a in args):
            raise ValueError('ip: sadece addr/route/link')
    elif base == 'nmcli':
        if not args or args[0] not in ('connection', 'con', 'c', 'device', 'dev', 'd', '-t'):
            raise ValueError('nmcli: sadece connection/device')
    elif base in ('dhclient', 'dhcpcd', 'udhcpc'):
        # betik calistirma secenekleri (-sf, -c, -s) yasak: sadece bilinen kaliplar
        allowed = {'dhclient': (['-r', None], [None]), 'dhcpcd': (['-n', None],),
                   'udhcpc': (['-i', None, '-n', '-q'],)}[base]
        if not any(len(pat) == len(args) and all(p == a if p else bool(iface_ok.match(a))
                                                 for p, a in zip(pat, args)) for pat in allowed):
            raise ValueError(f'{base}: izin verilmeyen secenek')
    elif base == 'ifconfig':
        if not args or not iface_ok.match(args[0]) or len(args) > 6:
            raise ValueError('ifconfig: gecersiz')
    path = which(base)
    if not path:
        raise ValueError(f'{base} bulunamadi')
    return [path] + argv[1:]


def helper_main(argv):
    """`bysterm --net-helper [in_fifo out_fifo]` — root olarak calisir."""
    if len(argv) >= 2:
        fin = open(argv[0], 'r')
        fout = open(argv[1], 'w')
    else:
        fin, fout = sys.stdin, sys.stdout
    os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
    fout.write(json.dumps({'ready': True, 'uid': os.geteuid() if not IS_WIN else 0}) + '\n')
    fout.flush()
    for line in fin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get('id')
        outs, ok = [], True
        try:
            cmds = [_helper_validate(c) for c in req.get('cmds', [])]
            for c in cmds:
                rc, out = run(c, timeout=120)
                outs.append(out.strip())
                if rc != 0:
                    ok = False
                    break
        except Exception as e:     # noqa: BLE001
            ok = False
            outs.append(f'yardimci: {e}')
        fout.write(json.dumps({'id': rid, 'ok': ok, 'out': '\n'.join(o for o in outs if o)}) + '\n')
        fout.flush()


def _self_cmd():
    """Bu programi yeniden calistiracak komut (paketli .exe/.app veya kaynak .py)."""
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    main = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bysterm.py')
    return [sys.executable, main]


class PrivHelper:
    """Yonetici yardimcisinin istemci tarafi (tekil)."""
    instance = None

    def __init__(self):
        self.proc = None
        self.fin = self.fout = None
        self.lock = threading.Lock()
        self.ok = False
        self.err = ''
        self._id = 0
        self._tmpdir = None

    @classmethod
    def get(cls):
        if cls.instance is None:
            cls.instance = cls()
        return cls.instance

    @property
    def alive(self):
        if not self.ok:
            return False
        if self.proc is not None and self.proc.poll() is not None:
            self.ok = False
        return self.ok

    def start(self, timeout=180):
        """Yetki iste (kullanici sifre penceresi gorur). Basariliysa True. Bloklar -> thread'de cagirin."""
        if IS_WIN:
            self.err = 'Windows: uygulama yonetici olarak calistirilir'
            return False
        if os.geteuid() == 0:
            self.ok = True       # zaten root: dogrudan calistir
            return True
        try:
            if IS_MAC:
                return self._start_mac(timeout)
            return self._start_linux(timeout)
        except Exception as e:     # noqa: BLE001
            self.err = str(e)
            self.ok = False
            return False

    def _start_linux(self, timeout):
        pk = which('pkexec')
        if not pk:
            self.err = 'pkexec yok'
            return False
        self.proc = subprocess.Popen([pk] + _self_cmd() + ['--net-helper'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True, bufsize=1)
        self.fin, self.fout = self.proc.stdout, self.proc.stdin
        return self._wait_ready(timeout)

    def _start_mac(self, timeout):
        import tempfile
        d = tempfile.mkdtemp(prefix='bysterm_')
        os.chmod(d, 0o700)
        fi, fo = os.path.join(d, 'in'), os.path.join(d, 'out')
        os.mkfifo(fi, 0o600)
        os.mkfifo(fo, 0o600)
        self._tmpdir = d
        cmd = ' '.join(shlex.quote(x) for x in _self_cmd() + ['--net-helper', fi, fo])
        sh = f'{cmd} >/dev/null 2>&1 &'
        esc = sh.replace('\\', '\\\\').replace('"', '\\"')
        rc, out = run(['osascript', '-e', f'do shell script "{esc}" with administrator privileges'],
                      timeout=timeout)
        if rc != 0:
            self.err = 'iptal edildi' if '-128' in out else out.strip()
            return False
        box = {}

        def opener():
            try:
                box['w'] = open(fi, 'w')
                box['r'] = open(fo, 'r')
            except OSError as e:
                box['e'] = e
        t = threading.Thread(target=opener, daemon=True)
        t.start()
        t.join(15)
        if 'r' not in box:
            self.err = 'yardimci baslamadi'
            return False
        self.fout, self.fin = box['w'], box['r']
        return self._wait_ready(15)

    def _wait_ready(self, timeout):
        box = {}

        def rd():
            box['line'] = self.fin.readline()
        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        line = box.get('line') or ''
        try:
            self.ok = bool(json.loads(line).get('ready'))
        except ValueError:
            self.ok = False
        if not self.ok:
            if self.proc is not None:
                try:
                    err = self.proc.stderr.read() if self.proc.poll() is not None else ''
                except Exception:
                    err = ''
                self.err = 'yetki verilmedi' if self.proc.poll() in (126, 127) else (err.strip() or 'yardimci yanit vermedi')
                try:
                    self.proc.kill()
                except Exception:
                    pass
        return self.ok

    def run(self, cmds, timeout=180):
        if os.geteuid() == 0:
            outs = []
            for c in cmds:
                try:
                    c = _helper_validate(c)
                except ValueError as e:
                    return False, str(e)
                rc, out = run(c, timeout=timeout)
                outs.append(out.strip())
                if rc != 0:
                    return False, '\n'.join(outs)
            return True, '\n'.join(outs)
        with self.lock:
            self._id += 1
            try:
                self.fout.write(json.dumps({'id': self._id, 'cmds': cmds}) + '\n')
                self.fout.flush()
                line = self.fin.readline()
                r = json.loads(line)
                return bool(r.get('ok')), r.get('out', '')
            except Exception as e:     # noqa: BLE001
                self.ok = False
                return False, f'yardimci baglantisi koptu: {e}'

    def grant_serial(self, device):
        """Linux: seri port erisimi — portu hemen ac (chmod) + kalici olarak dialout grubuna ekle."""
        user = os.environ.get('USER') or os.environ.get('LOGNAME') or ''
        if not user:
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        cmds = [['chmod', 'a+rw', device]]
        if IS_LINUX and _USER_RE.match(user):
            cmds.append(['usermod', '-aG', 'dialout', user])
        return self.run(cmds)

    def close(self):
        for f in (self.fout, self.fin):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        if self._tmpdir:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        self.ok = False


def relaunch_as_admin(extra_args=('--elevated',)):
    """Windows: kendini UAC ile yonetici olarak yeniden baslat. Basariliysa True (cagiran cikmali)."""
    if not IS_WIN:
        return False
    import ctypes
    cmd = _self_cmd()
    args = cmd[1:] + [a for a in sys.argv[1:] if a not in extra_args] + list(extra_args)
    r = ctypes.windll.shell32.ShellExecuteW(None, 'runas', cmd[0], subprocess.list2cmdline(args), None, 1)
    return int(r) > 32


# =========================================================================== Windows: com0com sanal port surucusu
# Seri izleme (sanal port koprusu) icin ucretsiz/imzali com0com. BYSTerm, com0com'u kullanicinin
# PC'sinde indirip kurabilir ve bir sanal port cifti olusturabilir (SourceForge kullanicinin aginda erisilir).
COM0COM_PAGE = 'https://com0com.sourceforge.net/'
COM0COM_ZIP = ('https://sourceforge.net/projects/com0com/files/com0com/3.0.0.0/'
               'com0com-3.0.0.0-i386-and-x64-signed.zip/download')


def com0com_setupc():
    """Kurulu com0com'un setupc.exe yolu (yoksa None)."""
    if not IS_WIN:
        return None
    for env in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, 'com0com', 'setupc.exe')
            if os.path.isfile(p):
                return p
    return None


def com0com_pairs():
    """Mevcut sanal port ciftleri -> [(portA, portB), ...]. com0com yoksa []."""
    sc = com0com_setupc()
    if not sc:
        return []
    rc, out = run([sc, 'list'], timeout=20)
    if rc != 0:
        return []
    # cikti: "CNCA0 PortName=COM5" / "CNCB0 PortName=COM6" ... ciftler numaraya gore eslesir
    a, b = {}, {}
    for line in out.splitlines():
        m = re.match(r'\s*CNC([AB])(\d+)\s+PortName=(\S+)', line)
        if m:
            name = m.group(3)
            if name == '-':
                name = f'CNC{m.group(1)}{m.group(2)}'
            (a if m.group(1) == 'A' else b)[m.group(2)] = name
    return [(a[k], b[k]) for k in sorted(a) if k in b]


def com0com_create_pair():
    """Yeni bir sanal port cifti olustur -> (basarili, metin). Yonetici gerekir."""
    sc = com0com_setupc()
    if not sc:
        return False, 'com0com kurulu degil'
    cmd = [sc, 'install', 'PortName=COM#', 'PortName=COM#']
    if is_admin():
        rc, out = run(cmd, timeout=60)
        return rc == 0, out
    # UAC ile setupc.exe'yi yonetici calistir
    import ctypes
    params = subprocess.list2cmdline(cmd[1:])
    r = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sc, params, os.path.dirname(sc), 0)
    if int(r) <= 32:
        return False, 'Iptal edildi veya yetki verilmedi'
    time.sleep(2.0)
    return True, 'cift olusturuldu (liste yenilenince gorunur)'


def com0com_install(progress=None, log=None):
    """com0com'u indir ve sessizce kur (UAC). -> (basarili, metin). Yalniz Windows."""
    if not IS_WIN:
        return False, 'Yalniz Windows'
    import tempfile
    import zipfile
    import urllib.request
    import ssl
    def say(m):
        if log:
            log(m)
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        say('com0com indiriliyor...')
        req = urllib.request.Request(COM0COM_ZIP, headers={'User-Agent': 'Mozilla/5.0'})
        tmp = tempfile.mkdtemp(prefix='bysterm_c0c_')
        zp = os.path.join(tmp, 'com0com.zip')
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r, open(zp, 'wb') as f:
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
        with open(zp, 'rb') as f:
            if f.read(2) != b'PK':
                return False, ('Indirme bir ZIP degil (SourceForge ara sayfasi gelmis olabilir). '
                               'Lutfen com0com.sourceforge.net adresinden elle kurun.')
        with zipfile.ZipFile(zp) as z:
            z.extractall(tmp)
        setup = None
        for root_, _, files in os.walk(tmp):
            if 'setup.exe' in files:
                setup = os.path.join(root_, 'setup.exe')
                break
        if not setup:
            return False, 'setup.exe bulunamadi'
        say('Kurulum calisiyor (UAC onayi gerekebilir)...')
        import ctypes
        from ctypes import wintypes

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
        sei.lpFile = setup
        sei.lpParameters = '/S'
        sei.lpDirectory = os.path.dirname(setup)
        sei.nShow = 1
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            err = ctypes.GetLastError()
            return (False, 'Iptal edildi (UAC)') if err == 1223 else (False, f'Kurulum baslatilamadi ({err})')
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 180000)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        if com0com_setupc():
            return True, 'com0com kuruldu'
        return True, 'Kurulum tamamlandi (yeniden tarayin)'
    except Exception as e:     # noqa: BLE001
        return False, f'Hata: {e}'
