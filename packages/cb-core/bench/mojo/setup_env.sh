#!/usr/bin/env bash
# Build the comparison sandbox: one interpreter, three builds of the same modules.
#
# .sandbox/py/  the cb_core hot modules, uncompiled
# .sandbox/cy/  the same files, Cython-compiled with the production directives
# .sandbox/cb_hot.so  the Mojo port
set -euo pipefail

cd "$(dirname "$0")"
SANDBOX=".sandbox"
SRC="../../src/cb_core"
MODULES=(cooldowns.py textmatch.py dedupe.py)

mkdir -p "$SANDBOX/py" "$SANDBOX/cy"

if [ ! -x "$SANDBOX/.venv/bin/mojo" ]; then
    uv venv --python 3.12 "$SANDBOX/.venv"
    # Mojo ships in Modular's `modular` wheel; PyPI is still needed for its deps.
    VIRTUAL_ENV="$PWD/$SANDBOX/.venv" uv pip install \
        --index-url https://dl.modular.com/public/max/python/simple/ \
        --extra-index-url https://pypi.org/simple \
        modular
    VIRTUAL_ENV="$PWD/$SANDBOX/.venv" uv pip install cython blake3 setuptools
fi

for module in "${MODULES[@]}"; do
    cp "$SRC/$module" "$SANDBOX/py/$module"
    cp "$SRC/$module" "$SANDBOX/cy/$module"
done
cp noopmod.py "$SANDBOX/py/noopmod.py"
cp noopmod.py "$SANDBOX/cy/noopmod.py"
touch "$SANDBOX/py/__init__.py" "$SANDBOX/cy/__init__.py"

cp setup_cy.py verify.py bench_all.py bench_call.py bench_batch2.py "$SANDBOX/"
cp cb_hot.mojo bench_native.mojo profile_textmatch.mojo "$SANDBOX/"

(cd "$SANDBOX" && ./.venv/bin/python setup_cy.py build_ext --inplace >/dev/null)
(cd "$SANDBOX" && ./.venv/bin/mojo build --emit shared-lib cb_hot.mojo -o cb_hot.so)

echo "sandbox ready — run ./run.sh"
