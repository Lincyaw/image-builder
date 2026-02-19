#!/usr/bin/env bash
#
# pandas_install.sh
#
# Tries three different sets of pinned Python/NumPy/Cython/etc. versions
# to build (and import) pandas. Exits on the first combination that succeeds.

# Stop on any error
set -e

# Quick mode: reuse existing venv, rebuild C extensions for the checked-out commit
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    # Clean generated files (redundant if Dockerfile uses git clean -fdx, but safe)
    git clean -fdx -e '.venv' 2>/dev/null || true
    # Only remove pyproject.toml if setup.py exists (old-style builds)
    if [ -f "setup.py" ]; then
        rm -f pyproject.toml
        # Try direct build first; if C compilation fails (incompatible commit),
        # fall back to setup.py develop, then full rebuild
        if CFLAGS="-O0 -Wno-error=array-bounds" .venv/bin/python setup.py build_ext --inplace -j 4 2>/dev/null; then
            exit 0
        fi
        if .venv/bin/python setup.py develop --no-deps 2>/dev/null; then
            exit 0
        fi
    else
        if uv pip install -e . 2>/dev/null; then
            exit 0
        fi
    fi
    echo "[INFO] Quick rebuild failed, falling back to full rebuild..."
    # Fall through to full install below
fi

########################
# Setup the "versioneer" command macro
########################
VERSIONEER_COMMAND='echo -e "[versioneer]\nVCS = git\nstyle = pep440\nversionfile_source = pandas/_version.py\nversionfile_build = pandas/_version.py\ntag_prefix =\nparentdir_prefix = pandas-" > setup.cfg && versioneer install'

########################
# Helper function to build & check
########################
build_and_check_pandas() {
  local python_ver="$1"
  local numpy_expr="$2"
  local cython_expr="$3"
  local setuptools_expr="$4"
  local versioneer_expr="$5"

  echo ""
  echo "[INFO] Creating new virtual environment with Python ${python_ver} ..."
  uv venv --clear --python "${python_ver}" --python-preference only-managed || return 1

  # Activate the new environment
  source .venv/bin/activate

  echo "[INFO] Upgrading pip and wheel ..."
  uv pip install --upgrade pip wheel

  echo "[INFO] Installing pinned dependencies ..."
  uv pip install --upgrade \
    "setuptools==${setuptools_expr}" \
    "numpy==${numpy_expr}" \
    "cython${cython_expr}" \
    "versioneer==${versioneer_expr}" \
    python-dateutil pytz pytest hypothesis jinja2

  if [ -f "requirements-dev.txt" ]; then
    uv pip install -r requirements-dev.txt || true
  fi

  echo "[INFO] Running versioneer setup ..."
  # The versioneer script is placed inline:
  bash -c "set -e; source .venv/bin/activate && ${VERSIONEER_COMMAND}" || true

  # Only remove pyproject.toml for old-style builds that have setup.py
  if [ -f "setup.py" ]; then
    echo "[INFO] Removing pyproject.toml if present (for older builds) ..."
    rm -f pyproject.toml

    echo "[INFO] Cleaning pandas build ..."
    .venv/bin/python setup.py clean --all || true

    echo "[INFO] Building pandas with CFLAGS='-O0 -Wno-error=array-bounds' ..."
    CFLAGS="-O0 -Wno-error=array-bounds" .venv/bin/python setup.py build_ext --inplace -j 4

    echo "[INFO] Installing pandas in editable mode ..."
    uv pip install -e . --no-build-isolation --no-deps
  else
    echo "[INFO] No setup.py found, trying pyproject.toml build ..."
    uv pip install -e . --no-build-isolation || uv pip install -e .
  fi

  echo "[INFO] Checking import of pandas ..."

  # IMPORTANT: Return 1 if import fails, so the function signals failure
  if ! .venv/bin/python -c "import pandas; print('Pandas version:', pandas.__version__); print(pandas.DataFrame([[1,2,3]]))"; then
    echo "[ERROR] Pandas import failed!"
    return 1
  fi

  echo "[SUCCESS] Build and import succeeded with Python=${python_ver}, NumPy=${numpy_expr}, Cython${cython_expr}."
}

########################
# Attempt #1: Python 3.8 + oldest NumPy (for pre-2020 pandas that needs NUMPY_IMPORT_ARRAY_RETVAL)
########################
echo "[Attempt #1] Trying Python=3.8, NumPy=1.18.*, Cython<0.30, setuptools=62.*, versioneer=0.23"
if build_and_check_pandas "3.8" "1.18.*" "<0.30" "62.*" "0.23"; then
  echo "[INFO] First combo succeeded. Exiting."
  exit 0
fi

########################
# Attempt #2: Python 3.8 (compatible with pandas 0.25-1.3)
########################
echo "[Attempt #2] Trying Python=3.8, NumPy=1.20.*, Cython<0.30, setuptools=62.*, versioneer=0.23"
if build_and_check_pandas "3.8" "1.20.*" "<0.30" "62.*" "0.23"; then
  echo "[INFO] Second combo succeeded. Exiting."
  exit 0
fi

########################
# Attempt #3: Python 3.9
########################
echo "[Attempt #3] Trying Python=3.9, NumPy=1.22.*, Cython<0.30, setuptools=62.*, versioneer=0.23"
if build_and_check_pandas "3.9" "1.22.*" "<0.30" "62.*" "0.23"; then
  echo "[INFO] Third combo succeeded. Exiting."
  exit 0
fi

########################
# Attempt #4: Python 3.10 (for newer pandas with meson/pyproject.toml)
########################
echo "[Attempt #4] Trying Python=3.10, NumPy=1.26.*, Cython===3.0.5, setuptools=62.*, versioneer=0.23"
if build_and_check_pandas "3.10" "1.26.*" "===3.0.5" "62.*" "0.23"; then
  echo "[INFO] Fourth combo succeeded. Exiting."
  exit 0
fi

########################
# If none succeeded
########################
echo "[ERROR] All four attempts failed."
exit 1
