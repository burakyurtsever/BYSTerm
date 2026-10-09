# -*- mode: python -*-
# PyInstaller tarifi — ci/build.py tarafindan cagrilir (dogrudan: pyinstaller ci/bysterm.spec)
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
NAME = 'BYSTerm'
ICON = os.path.join(ROOT, 'assets', {'win32': 'icon.ico', 'darwin': 'icon.icns'}.get(sys.platform, 'icon.png'))

a = Analysis(
    [os.path.join(ROOT, 'src', 'bysterm.py')],
    pathex=[os.path.join(ROOT, 'src')],
    datas=[(os.path.join(ROOT, 'assets'), 'assets')],
    hiddenimports=['serial.tools.list_ports'],
    # tek Qt baglamasi: PyQt5 (en eski sistemlere kadar destek)
    excludes=['PySide6', 'PySide2', 'tkinter', 'PyQt5.QtNetwork', 'PyQt5.QtQml',
              'PyQt5.QtQuick', 'PyQt5.QtWebEngineCore', 'PyQt5.QtMultimedia', 'PyQt5.QtSql'],
)

if sys.platform.startswith('linux'):
    # Font kutuphaneleri sistemden gelsin: eski (18.04) libfontconfig yeni sistemlerin
    # fonts.conf dosyasini okuyamayip uyari basiyor. Tum masaustu/Jetson'da zaten var.
    _skip = ('libfontconfig.so', 'libfreetype.so', 'libexpat.so', 'libuuid.so', 'libz.so',
             'libpng16.so')
    a.binaries = [b for b in a.binaries if not os.path.basename(b[0]).startswith(_skip)]

pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, icon=ICON)
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME)
    app = BUNDLE(coll, name=NAME + '.app', icon=ICON, bundle_identifier='com.bys.bysterm',
                 info_plist={'NSHighResolutionCapable': True,
                             'CFBundleShortVersionString': os.environ.get('BYSTERM_VERSION', '1.0.0')})
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=NAME, console=False, icon=ICON,
              upx=False, runtime_tmpdir=None)
