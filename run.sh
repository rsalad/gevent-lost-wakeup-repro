#!/bin/sh
# Build the three images and run every scenario N times in each (default 200).
# usage: ./run.sh [N]
set -eu
cd "$(dirname "$0")"
N=${1:-200}
for build in stock source fixed; do
    docker build -q --target "$build" -t "gevent-lost-wakeup:$build" . >/dev/null
done
for build in stock source fixed; do
    docker run --rm "gevent-lost-wakeup:$build" python runs.py "$N" || true
    docker run --rm -e PURE_PYTHON=1 "gevent-lost-wakeup:$build" python runs.py "$N" || true
done
