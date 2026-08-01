#!/usr/bin/env bash
set -o errexit

echo "════════════════════════════════════════"
echo "🚀 Cambodia Legal Bot v17.5"
echo "════════════════════════════════════════"

# Render provides apt but requires special syntax
echo "📦 Installing WeasyPrint dependencies via apt..."

# ─── Update apt sources (Render allows this) ───
apt-get update -qq || true

# ─── Try install system packages ───────────────
apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    fonts-khmeros \
    fonts-noto \
    fontconfig 2>/dev/null || echo "⚠️ apt failed - using bundled fonts"

# ─── Refresh font cache ────────────────────────
fc-cache -f -v 2>/dev/null || true

# ─── Download fonts as backup ──────────────────
mkdir -p fonts
if [ ! -f "fonts/NotoSansKhmer-Regular.ttf" ]; then
    echo "📥 Downloading Noto Sans Khmer..."
    curl -sL -o fonts/NotoSansKhmer-Regular.ttf \
      "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansKhmer/NotoSansKhmer-Regular.ttf"
fi

# ─── Verify ────────────────────────────────────
echo "📋 Available fonts:"
fc-list 2>/dev/null | grep -i khmer || echo "System fonts: none"
ls -lh fonts/

# ─── Install Python packages ───────────────────
echo ""
echo "🐍 Installing Python Packages..."
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "✅ Build Complete!"
