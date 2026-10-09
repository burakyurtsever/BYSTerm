#!/usr/bin/env python3
"""BYSTerm'i tek dosya uygulama olarak paketler (PyInstaller).

  python ci/build.py            -> release/BYSTerm-<isletim-sistemi>-<mimari>.<zip|exe|tar.gz>

Windows : BYSTerm-windows-x64.exe        (tek dosya, cift tikla)
macOS   : BYSTerm-macos-<arm64|x64>.zip  (icinde BYSTerm.app)
Linux   : BYSTerm-linux-<x64|arm64>.tar.gz (icinde tek dosya BYSTerm)
"""
import os
import sys
import shutil
import platform
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
NAME = 'BYSTerm'


def arch():
    m = platform.machine().lower()
    return {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(m, m)


def run(cmd):
    print('+', ' '.join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)


def main():
    for d in ('build', 'dist'):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
         os.path.join('ci', 'bysterm.spec')])

    out = os.path.join(ROOT, 'release')
    os.makedirs(out, exist_ok=True)
    a = arch()
    if sys.platform == 'win32':
        dst = os.path.join(out, f'{NAME}-windows-{a}.exe')
        shutil.copy2(os.path.join(ROOT, 'dist', f'{NAME}.exe'), dst)
    elif sys.platform == 'darwin':
        dst = os.path.join(out, f'{NAME}-macos-{a}.zip')
        run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
             os.path.join('dist', f'{NAME}.app'), dst])
    else:
        # klasor: BYSTerm/ {BYSTerm, bysterm.png, install.sh}
        stage = os.path.join(ROOT, 'dist', 'pkg', NAME)
        os.makedirs(stage)
        shutil.copy2(os.path.join(ROOT, 'dist', NAME), os.path.join(stage, NAME))
        shutil.copy2(os.path.join(ROOT, 'assets', 'icon.png'), os.path.join(stage, 'bysterm.png'))
        shutil.copy2(os.path.join(ROOT, 'ci', 'linux_install.sh'), os.path.join(stage, 'install.sh'))
        dst = os.path.join(out, f'{NAME}-linux-{a}.tar.gz')
        run(['tar', '-C', os.path.join('dist', 'pkg'), '-czf', dst, NAME])
    print('OK ->', dst, f'({os.path.getsize(dst) / 1e6:.1f} MB)')


if __name__ == '__main__':
    main()
