"""
BYSTerm otomatik guncelleme — Qt'den bagimsiz.

GitHub Releases'tan en son surumu sorar; daha yeniyse bu sisteme uygun dosyayi indirir ve
calisan uygulamanin yerine koyar:
  Windows : BYSTerm.exe -> calisan exe yeniden adlandirilir (.old), yenisi yerine konur
  Linux   : tek dosya BYSTerm atomik olarak degistirilir (calisirken mumkun)
  macOS   : BYSTerm.app paketi degistirilir
Kaynak koddan calisirken sadece surum sayfasi acilir.
"""
import os
import sys
import json
import shutil
import platform
import tempfile
import subprocess

REPO = 'burakyurtsever/BYSTerm'
API = f'https://api.github.com/repos/{REPO}/releases/latest'
PAGE = f'https://github.com/{REPO}/releases/latest'

IS_WIN = sys.platform.startswith('win')
IS_MAC = sys.platform == 'darwin'


def parse_version(v):
    out = []
    for p in str(v).lstrip('vV').split('.'):
        num = ''.join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _ssl_context():
    import ssl
    ctx = ssl.create_default_context()
    try:      # paketli uygulamada sistem sertifikalari bulunamayabilir (ozellikle macOS)
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def _open(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'BYSTerm-updater',
                                               'Accept': 'application/vnd.github+json'})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def latest_release():
    """-> {'version': '1.2.0', 'notes': str, 'assets': {ad: url}, 'page': url}"""
    with _open(API) as r:
        d = json.loads(r.read().decode('utf-8'))
    return {'version': d.get('tag_name', '').lstrip('vV'),
            'notes': d.get('body') or '',
            'assets': {a['name']: a['browser_download_url'] for a in d.get('assets', [])},
            'page': d.get('html_url') or PAGE}


def asset_name():
    m = platform.machine().lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(m, m)
    if IS_WIN:
        return 'BYSTerm-windows-x64.exe'
    if IS_MAC:
        return f'BYSTerm-macos-{arch}.zip'
    return f'BYSTerm-linux-{arch}.tar.gz'


def is_frozen():
    return bool(getattr(sys, 'frozen', False))


def install_target():
    """Degistirilecek dosya/paket yolu."""
    exe = os.path.abspath(sys.executable)
    if IS_MAC:
        p = exe
        while p and p != '/' and not p.endswith('.app'):
            p = os.path.dirname(p)
        return p if p.endswith('.app') else exe
    return exe


def download(url, dst, progress=None, cancelled=None):
    with _open(url, timeout=30) as r, open(dst, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        done = 0
        while True:
            if cancelled is not None and cancelled():
                raise RuntimeError('cancelled')
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return dst


def install(pkg_path):
    """Indirilen paketi kur -> yeniden baslatilacak komut (liste)."""
    target = install_target()
    tdir = os.path.dirname(target)
    if not os.access(tdir, os.W_OK):
        raise PermissionError(tdir)
    if IS_WIN:
        old = target[:-4] + '.old.exe' if target.lower().endswith('.exe') else target + '.old'
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            pass
        os.replace(target, old)            # calisan exe yeniden adlandirilabilir
        shutil.move(pkg_path, target)
        return [target, '--updated']
    if IS_MAC:
        work = tempfile.mkdtemp(prefix='bysterm_upd_')
        subprocess.check_call(['ditto', '-x', '-k', pkg_path, work])
        apps = [n for n in os.listdir(work) if n.endswith('.app')]
        if not apps:
            raise RuntimeError('zip icinde .app yok')
        new_app = os.path.join(work, apps[0])
        backup = target + '.old'
        shutil.rmtree(backup, ignore_errors=True)
        os.rename(target, backup)
        shutil.move(new_app, target)
        shutil.rmtree(backup, ignore_errors=True)
        return ['open', '-n', target, '--args', '--updated']
    # Linux: tar.gz icindeki BYSTerm/BYSTerm
    import tarfile
    work = tempfile.mkdtemp(prefix='bysterm_upd_', dir=tdir)
    with tarfile.open(pkg_path) as tf:
        member = next((m for m in tf.getmembers() if m.isfile() and os.path.basename(m.name) == 'BYSTerm'), None)
        if member is None:
            raise RuntimeError('arsivde BYSTerm yok')
        member.name = 'BYSTerm'
        tf.extract(member, work)
    new = os.path.join(work, 'BYSTerm')
    os.chmod(new, 0o755)
    os.replace(new, target)                # calisan dosya atomik degistirilebilir
    shutil.rmtree(work, ignore_errors=True)
    return [target, '--updated']


def cleanup_old():
    """Onceki guncellemeden kalan .old dosyasini sil (Windows)."""
    if IS_WIN and is_frozen():
        t = install_target()
        old = t[:-4] + '.old.exe' if t.lower().endswith('.exe') else t + '.old'
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            pass


def linux_desktop_integration(icon_src):
    """Linux: paketli uygulama kendini uygulama menusune (simgesiyle) ekler / yolunu gunceller."""
    if not sys.platform.startswith('linux') or not is_frozen():
        return False
    try:
        exe = os.path.abspath(sys.executable)
        home = os.path.expanduser('~')
        apps = os.path.join(home, '.local', 'share', 'applications')
        icons = os.path.join(home, '.local', 'share', 'icons', 'hicolor', '256x256', 'apps')
        os.makedirs(apps, exist_ok=True)
        os.makedirs(icons, exist_ok=True)
        icon = os.path.join(icons, 'bysterm.png')
        if os.path.exists(icon_src):
            shutil.copyfile(icon_src, icon)
        desk = os.path.join(apps, 'bysterm.desktop')
        content = ('[Desktop Entry]\nType=Application\nName=BYSTerm\n'
                   'Comment=Serial / TCP / UDP / network test toolkit\n'
                   f'Exec="{exe}"\nIcon={icon}\nTerminal=false\nCategories=Development;Utility;Network;\n'
                   'StartupWMClass=BYSTerm\n')
        old = ''
        if os.path.exists(desk):
            with open(desk) as f:
                old = f.read()
        if old != content:
            with open(desk, 'w') as f:
                f.write(content)
            os.chmod(desk, 0o755)
            return True
    except Exception:
        pass
    return False
