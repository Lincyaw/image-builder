
set -e

# Quick mode: try to reuse existing venv; fall through to full install on failure
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    uv pip install "setuptools<58.0" 2>/dev/null || true
    if [ -f "_datalad_build_support/setup.py" ]; then
        sed -i "s/platform\.dist()/('', '', '')/g" _datalad_build_support/setup.py 2>/dev/null || true
    fi
    if uv pip install --no-build-isolation -e . 2>/dev/null; then
        exit 0
    fi
    echo "[INFO] Quick install failed, falling back to full install..."
fi

check_install() {
    echo "Verifying installation..."
    if python -c "import datalad; print('Datalad version:', datalad.__version__)"; then
        echo "✅ Installation successful!"
        return 0
    else
        echo "❌ Verification failed"
        return 1
    fi
}

test_39_install () {
    uv venv --python 3.9
    source .venv/bin/activate

    uv pip install setuptools "pytest<8" pytest-cov numpy 'pybids<0.7.0' nose
    uv pip install -e .[full]
    uv pip install -e .[devel]

    check_install
}


test_37_install () {
    uv venv --python 3.7
    source .venv/bin/activate

    uv pip install setuptools "pytest<8" pytest-cov numpy 'pybids<0.7.0' fasteners bids nose
    uv pip install -e .[full]
    uv pip install -e .[devel]

    check_install
}


echo "Starting Datalad installation attempts..."

# Try Python 3.9 installation
if test_39_install; then
    echo "Successfully installed Datalad using Python 3.9"
    exit 0
fi

echo "Python 3.9 installation failed, trying Python 3.7..."

# Try Python 3.7 installation
if test_37_install; then
    echo "Successfully installed Datalad using Python 3.7"
    exit 0
fi