#!/bin/sh
# Remove every integration installed by install.sh. Message data is kept unless --purge-data.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
/usr/bin/python3 -I "$ROOT/scripts/installer.py" uninstall "$@"
