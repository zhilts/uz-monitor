#!/bin/zsh
set -eu
cd "${0:A:h}"
exec /usr/bin/python3 uz_monitor.py once

