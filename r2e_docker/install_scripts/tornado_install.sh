# Quick mode: reuse existing venv, just reinstall editable package
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    uv pip install -e .
    exit 0
fi

uv venv --python=python3.9
source .venv/bin/activate

uv pip install -e .
uv pip install pycares pycurl twisted tox pytest
