#!/usr/bin/env bash
# Ubuntu 18.04 (glibc 2.27) icinde derler -> cikan uygulama 18.04 / 20.04 / 22.04 / 24.04+
# ve tum JetPack'lerde (4.x, 5.x, 6.x) calisir. Hem x64 hem arm64 (Jetson) icin ayni betik.
#   docker run --rm -v "$PWD":/src -w /src ubuntu:18.04 bash ci/build_linux_docker.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8
apt-get update -q
apt-get install -y -q --no-install-recommends \
    python3 python3-pip python3-setuptools python3-wheel python3-dev \
    python3-pyqt5 python3-serial binutils gcc zlib1g-dev \
    libxkbcommon-x11-0 libxcb-xinerama0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 ca-certificates
python3 -m pip install -q "pip==21.3.1"
python3 -m pip install -q "pyinstaller==4.10" "pyinstaller-hooks-contrib==2022.0"
python3 --version
python3 -c "from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR; print('Qt', QT_VERSION_STR, 'PyQt', PYQT_VERSION_STR)"
python3 ci/build.py
chown -R "${HOST_UID:-0}:${HOST_GID:-0}" release dist build 2>/dev/null || true
