# Quick mode: try to reuse existing venv; fall through to full install on failure
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    if uv pip install -e . 2>/dev/null; then
        exit 0
    fi
    echo "[INFO] Quick install failed, falling back to full install..."
fi

uv venv --python=python3.9
source .venv/bin/activate

uv pip install -e .
uv pip install pycares pycurl twisted tox pytest
