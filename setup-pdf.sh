#!/bin/bash
# Setup PDF export dependencies for Obsidian Viewer
# Run this if PDF export stops working (e.g., after system reinstall)

VENV_PATH="$HOME/clawd/envs/pdfenv"

echo "📄 Setting up PDF export environment..."

# Create venv if it doesn't exist
if [ ! -d "$VENV_PATH" ]; then
    echo "Creating virtual environment at $VENV_PATH..."
    mkdir -p "$HOME/clawd/envs"
    python3 -m venv "$VENV_PATH"
fi

# Install/upgrade weasyprint and dependencies
echo "Installing weasyprint and markdown..."
"$VENV_PATH/bin/pip" install --upgrade pip weasyprint markdown -q

# Verify installation
if "$VENV_PATH/bin/python3" -c "import weasyprint; print('✅ weasyprint installed successfully')" 2>/dev/null; then
    echo "✅ PDF export is ready!"
    echo "   Location: $VENV_PATH"
else
    echo "❌ Installation failed. Check error messages above."
    exit 1
fi
