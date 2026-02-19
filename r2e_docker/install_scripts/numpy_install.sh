#!/bin/bash

set -e

# Clean ALL generated/untracked files from previous builds.
# Critical: __config__.py files left from the reference commit cause _INSTALLED=True
# in numpy/distutils/__init__.py, which triggers circular imports via numpy.testing.
# Also removes stale .so files, .pxd files from newer numpy, etc.
# Using git clean -fdx ensures a pristine source tree matching the checked-out commit.
git clean -fdx -e '.venv' 2>/dev/null || true

# Quick mode: try to reuse existing venv, fall back to full rebuild if needed
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    # Pin setuptools: old numpy commits use legacy build backend without build_editable
    uv pip install "setuptools==59.8.0" 2>/dev/null || true
    # Try quick rebuild: works when the checked-out commit is compatible with base venv
    if .venv/bin/python setup.py build_ext --inplace 2>/dev/null; then
        exit 0
    fi
    # Fallback: try non-editable install to avoid build_editable errors
    if [ -f "pyproject.toml" ] && grep -q "meson\|build-backend" pyproject.toml 2>/dev/null; then
        if uv pip install --no-build-isolation . 2>/dev/null; then
            exit 0
        fi
    fi
    echo "[INFO] Quick rebuild failed (numpy bootstrap problem), falling back to full rebuild..."
    # Fall through to full install below
fi

check_numpy() {
    if .venv/bin/python -c "import numpy; numpy.array([1,2])" &>/dev/null; then
        echo "✅ NumPy installation successful!"
        return 0
    else
        echo "❌ NumPy verification failed"
        return 1
    fi
}

try_install() {
    local pyver="$1"
    shift
    echo "--- Trying Python ${pyver} ---"
    uv venv --clear --python "${pyver}" --python-preference only-managed || return 1
    source .venv/bin/activate
    uv pip install "$@" || return 1
    if [ -f "pyproject.toml" ] && grep -q "meson\|build-backend" pyproject.toml 2>/dev/null; then
        uv pip install --no-build-isolation -e . || return 1
    else
        .venv/bin/python setup.py build_ext --inplace || return 1
    fi
    check_numpy
}

# Try in order: most compatible first for old numpy 1.x source builds.
# Python 3.8 + Cython 0.29.x is the sweet spot for numpy 1.x era commits.
# Python 3.9 as backup. Avoid 3.10+ for old source (C API breaking changes).
try_install 3.8 \
    "setuptools==59.8.0" "cython==0.29.37" "wheel" \
    pytest pytest-env hypothesis nose \
    && exit 0

try_install 3.9 \
    "setuptools==59.8.0" "cython==0.29.37" "wheel" \
    pytest pytest-env hypothesis nose \
    && exit 0

try_install 3.10 \
    "setuptools==59.8.0" "cython==0.29.37" "wheel" \
    pytest pytest-env hypothesis nose \
    && exit 0

echo "All installation attempts failed"
exit 1
