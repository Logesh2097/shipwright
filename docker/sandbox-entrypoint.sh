#!/bin/sh
# Runs one command inside the sandbox.
#  - activates the per-repository virtualenv in /deps when present
#  - optionally creates it first (dependency installation only)
#  - prints a report file between markers so the caller can read structured results
#    without sharing a writable directory with the container

if [ "${ASEA_PREPARE_VENV:-}" = "1" ] && [ ! -x /deps/venv/bin/python ]; then
    python -m venv --system-site-packages /deps/venv || exit 125
fi

if [ -x /deps/venv/bin/python ]; then
    VIRTUAL_ENV=/deps/venv
    PATH="/deps/venv/bin:$PATH"
    export VIRTUAL_ENV PATH
fi

if [ -d node_modules/.bin ]; then
    PATH="$PWD/node_modules/.bin:$PATH"
    export PATH
fi

"$@"
code=$?

if [ -n "${ASEA_REPORT_FILE:-}" ] && [ -f "$ASEA_REPORT_FILE" ]; then
    printf '\n===ASEA-REPORT-BEGIN===\n'
    cat "$ASEA_REPORT_FILE"
    printf '\n===ASEA-REPORT-END===\n'
fi

exit $code
