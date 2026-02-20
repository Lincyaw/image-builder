# Quick mode: try to reuse existing venv; fall through to full install on failure
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    if uv pip install -e . 2>/dev/null; then
        # Re-run asyncio migration if the script exists
        if [ -f "process_aiohttp_updateasyncio.py" ]; then
            .venv/bin/python process_aiohttp_updateasyncio.py 2>/dev/null || true
        fi
        exit 0
    fi
    echo "[INFO] Quick install failed, falling back to full install..."
fi

uv venv --python 3.9
source .venv/bin/activate

make .develop

uv pip install pytest pytest-asyncio pytest-cov pytest-asyncio pytest-mock coverage gunicorn async-generator brotlipy cython multdict yarl async-timeout trustme chardet

.venv/bin/python process_aiohttp_updateasyncio.py