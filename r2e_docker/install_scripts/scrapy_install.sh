# Quick mode: reuse existing venv, just reinstall editable package
if [ -d ".venv" ] && [ "$1" = "--quick" ]; then
    source .venv/bin/activate
    pip install -e . --no-deps 2>/dev/null || pip install -e .
    exit 0
fi

uv venv --python=python3.9
source .venv/bin/activate

if [ -f "requirements.txt" ]; then
    echo "Found requirements.txt file. Installing dependencies..."
    
    # Check if pip is installed
    uv pip install -r requirements.txt
    
    echo "Dependencies installation completed!"
else
    echo "No requirements.txt file found in the current directory."
fi

uv pip install -e .

uv pip install pytest testfixtures pyftpdlib pexpect