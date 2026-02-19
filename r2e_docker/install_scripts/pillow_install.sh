#!/bin/bash

set -e  # Exit on any error

# Quick mode: reuse existing venv, rebuild C extensions for the checked-out commit
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    # Clean old compiled files
    find . -name '*.pyc' -delete 2>/dev/null || true
    find . -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    uv pip install -e . --no-build-isolation
    exit 0
fi

check_pillow() {
    echo "Verifying Pillow installation..."
    if .venv/bin/python -c "import PIL; from PIL import Image; Image.new('RGB', (1, 1))" &> /dev/null; then
        echo "Pillow installation successful!"
        return 0
    else
        echo "Pillow verification failed"
        return 1
    fi
}

try_install() {
    local pyver="$1"
    echo "--- Trying Python ${pyver} ---"
    uv venv --clear --python "${pyver}" --python-preference only-managed || return 1
    source .venv/bin/activate
    uv pip install setuptools pytest pytest-cov PyQt5
    uv pip install -e . --no-build-isolation
    check_pillow
}

echo "Starting Pillow installation attempts..."

# Try Python 3.9 first (for older Pillow with setup.py)
if try_install 3.9; then
    echo "Successfully installed Pillow using Python 3.9"
    exit 0
fi

echo "Python 3.9 failed, trying Python 3.10..."

# Try Python 3.10 (for newer Pillow needing >=3.10)
if try_install 3.10; then
    echo "Successfully installed Pillow using Python 3.10"
    exit 0
fi

echo "All installation attempts failed"
exit 1