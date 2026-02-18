#!/bin/bash

set -e  # Exit on any error

# Quick mode: reuse existing venv, just rebuild extensions
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    pip install -e . --no-build-isolation --no-deps 2>/dev/null || pip install -e . --no-build-isolation 2>/dev/null || pip install -e .
    exit 0
fi

check_pillow() {
    echo "Verifying Pillow installation..."
    if python -c "import PIL; from PIL import Image; Image.new('RGB', (1, 1))" &> /dev/null; then
        echo "✅ Pillow installation successful!"
        return 0
    else
        echo "❌ Pillow verification failed"
        return 1
    fi
}

main() {
    echo "Starting Pillow installation attempts..."
    
    uv venv --python 3.9
    source .venv/bin/activate
    uv pip install setuptools pytest pytest-cov PyQt5
    uv pip install -e . --no-build-isolation

    if check_pillow; then
        echo "Successfully installed Pillow"
        return 0
    fi

    return 1
}

# Run the main function
main