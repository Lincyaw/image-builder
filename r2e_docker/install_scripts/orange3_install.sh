#!/bin/bash

set -e  # Exit on any error

# Quick mode: try to reuse existing venv; fall through to full install on failure
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    # Pin setuptools<60: numpy.distutils breaks with setuptools>=60
    uv pip install "setuptools<60" --force-reinstall 2>/dev/null || true
    uv pip install trubar 2>/dev/null || true
    find . -name '*.pyc' -delete 2>/dev/null || true
    find . -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    if .venv/bin/python setup.py build_ext --inplace 2>/dev/null \
       && .venv/bin/python setup.py develop 2>/dev/null; then
        exit 0
    fi
    echo "[INFO] Quick install failed, falling back to full install..."
fi

check_orange() {
    echo "Verifying Orange installation..."
    if .venv/bin/python -c "import Orange; print(Orange.__file__)"   &> /dev/null; then
        echo "✅ Orange installation successful!"
        ln -s Orange/tests/datasets/ datasets
        return 0
    else
        echo "❌ Orange verification failed"
        return 1
    fi
}


try_install_python37() {
    echo "Attempting installation with Python 3.7..."
    uv venv --clear --python 3.7 --python-preference only-managed || return 1
    source .venv/bin/activate
    uv pip install --upgrade "setuptools<60" "numpy<1.18" wheel "cython<0.30" pytest "PyQt5>=5.12,!=5.15.1" "PyQtWebEngine>=5.12"
    uv pip install -r requirements-core.txt
    uv pip install -r requirements-gui.txt
    uv pip install -r requirements-sql.txt
    if [ -f requirements-opt.txt ]; then
        uv pip install -r requirements-opt.txt
    fi
    uv pip install scipy scikit-learn
    .venv/bin/python setup.py build_ext --inplace
    .venv/bin/python setup.py develop
    # Re-pin setuptools after develop (pip may have upgraded it)
    uv pip install "setuptools<60" --force-reinstall
    check_orange
}

try_install_python38() {
    echo "Attempting installation with Python 3.8..."
    uv venv --clear --python 3.8 --python-preference only-managed || return 1
    source .venv/bin/activate
    uv pip install --upgrade "setuptools<60" "numpy<1.25" wheel "cython<0.30" pytest "PyQt5>=5.12,!=5.15.1" "PyQtWebEngine>=5.12"
    uv pip install -r requirements-core.txt
    uv pip install -r requirements-gui.txt
    uv pip install -r requirements-sql.txt 2>/dev/null || uv pip install -r requirements-sql.txt 2>/dev/null || true
    if [ -f requirements-opt.txt ]; then
        uv pip install -r requirements-opt.txt || true
    fi
    .venv/bin/python setup.py build_ext --inplace
    .venv/bin/python setup.py develop
    # Re-pin setuptools after develop (pip may have upgraded it)
    uv pip install "setuptools<60" --force-reinstall
    check_orange
}

try_install_python310() {
    echo "Attempting installation with Python 3.10..."
    uv venv --clear --python 3.10 --python-preference only-managed || return 1
    source .venv/bin/activate
    # setuptools<60 is required for numpy.distutils compatibility on Python 3.10
    uv pip install --upgrade "setuptools<60" numpy wheel cython pytest "PyQt5>=5.12,!=5.15.1" "PyQtWebEngine>=5.12"
    uv pip install -r requirements-core.txt
    uv pip install -r requirements-gui.txt
    if [ -f requirements-dev.txt ]; then
        uv pip install -r requirements-dev.txt || true
    fi
    uv pip install -r requirements-sql.txt 2>/dev/null || uv pip install -r requirements-sql.txt 2>/dev/null || true
    .venv/bin/python setup.py build_ext --inplace
    .venv/bin/python setup.py develop
    # Re-pin setuptools after develop (pip may have upgraded it)
    uv pip install "setuptools<60" --force-reinstall
    uv pip install -e . --no-binary=orange3 --no-deps 2>/dev/null || true
    check_orange
}

main() {
    echo "Starting Orange installation attempts..."
    
    # Try Python 3.7 installation
    if try_install_python37; then
        echo "Successfully installed orange using Python 3.7"
        return 0
    fi
    
    echo "Python 3.7 installation failed, trying Python 3.8..."

    # Try Python 3.8 installation
    if try_install_python38; then
        echo "Successfully installed orange using Python 3.8"
        return 0
    fi

    echo "Python 3.7,3.8 installation failed, trying Python 3.8..."

    # Try Python 3.11 installation
    if try_install_python310; then
        echo "Successfully installed orange using Python 3.10"
        return 0
    fi
    
    echo "All installation attempts failed"
    return 1
}

# Run the main function
main