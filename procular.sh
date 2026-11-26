#!/bin/sh
# Run Procular from the source tree.
HERE=$(dirname "$(readlink -f "$0")")
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m procular "$@"
