#!/usr/bin/env bash
# Paketlenmis uygulamayi temiz bir Ubuntu konteynerinde X11 (Xvfb) ile calistirir.
# Kurulan paketler her masaustu/Jetson imajinda zaten bulunan grafik kutuphaneleridir.
#   docker run --rm -v /yol/BYSTerm:/app -v $PWD/ci:/ci ubuntu:22.04 bash /ci/selftest_linux.sh
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq xvfb xauth libgl1 libgles2 libegl1 libfontconfig1 libglib2.0-0 libdbus-1-3 >/dev/null 2>&1 || \
  apt-get install -y -qq xvfb xauth libgl1-mesa-glx libgles2-mesa libfontconfig1 libglib2.0-0 libdbus-1-3 >/dev/null
. /etc/os-release
echo "== $PRETTY_NAME (glibc $(ldd --version | head -1 | awk '{print $NF}'), $(uname -m))"
xvfb-run -a timeout ${SELFTEST_TIMEOUT:-60} /app/BYSTerm --selftest
