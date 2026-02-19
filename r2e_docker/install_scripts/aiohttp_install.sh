# Quick mode: reuse existing venv, just reinstall editable package
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    uv pip install -e .
    # Re-run asyncio migration if the script exists
    if [ -f "process_aiohttp_updateasyncio.py" ]; then
        .venv/bin/python process_aiohttp_updateasyncio.py
    fi
    exit 0
fi

uv venv --python 3.9
source .venv/bin/activate

make .develop

uv pip install pytest pytest-asyncio pytest-cov pytest-asyncio pytest-mock coverage gunicorn async-generator brotlipy cython multdict yarl async-timeout trustme chardet

.venv/bin/python process_aiohttp_updateasyncio.py