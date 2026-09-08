#!/usr/bin/env bash
# Build blackboard-bus_<version>_all.deb. Run inside Ubuntu (WSL or a VM).
set -euo pipefail

VERSION=1.0.1
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${HOME}/build"
BUILD="${OUT}/blackboard-bus_${VERSION}"

rm -rf "$BUILD"
mkdir -p "$BUILD/DEBIAN" \
         "$BUILD/usr/lib/blackboard-bus" \
         "$BUILD/usr/bin" \
         "$BUILD/lib/systemd/system" \
         "$BUILD/etc/blackboard-bus" \
         "$BUILD/usr/share/doc/blackboard-bus"

install -m 0644 "$SRC/src/bus_server.py"                "$BUILD/usr/lib/blackboard-bus/bus_server.py"
install -m 0755 "$SRC/packaging/blackboard-bus"         "$BUILD/usr/bin/blackboard-bus"
install -m 0644 "$SRC/packaging/blackboard-bus.service" "$BUILD/lib/systemd/system/blackboard-bus.service"
install -m 0644 "$SRC/packaging/env.example"            "$BUILD/etc/blackboard-bus/env.example"
install -m 0644 "$SRC/docs/DEPLOY-GCP.md"               "$BUILD/usr/share/doc/blackboard-bus/DEPLOY-GCP.md"
install -m 0644 "$SRC/packaging/DEBIAN/control"         "$BUILD/DEBIAN/control"
install -m 0644 "$SRC/packaging/DEBIAN/conffiles"       "$BUILD/DEBIAN/conffiles"
install -m 0755 "$SRC/packaging/DEBIAN/preinst"         "$BUILD/DEBIAN/preinst" 2>/dev/null || true
install -m 0755 "$SRC/packaging/DEBIAN/prerm"           "$BUILD/DEBIAN/prerm"
install -m 0755 "$SRC/packaging/DEBIAN/postinst"        "$BUILD/DEBIAN/postinst"
install -m 0755 "$SRC/packaging/DEBIAN/postrm"          "$BUILD/DEBIAN/postrm"

dpkg-deb --build --root-owner-group "$BUILD" "${OUT}/blackboard-bus_${VERSION}_all.deb"
echo
dpkg-deb --info "${OUT}/blackboard-bus_${VERSION}_all.deb"
echo
dpkg-deb --contents "${OUT}/blackboard-bus_${VERSION}_all.deb"
