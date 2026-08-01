#!/usr/bin/env bash
# Build script for Render.com (No sudo needed)
set -o errexit

echo "════════════════════════════════════════"
echo "🚀 Cambodia Legal Bot v17.5 - Build"
echo "════════════════════════════════════════"

# ─── Create fonts directory ────────────────────
mkdir -p fonts

echo ""
echo "📥 Downloading Khmer Fonts..."
echo "════════════════════════════════════════"

# ─── Download Noto Sans Khmer (Regular) ────────
if [ ! -f "fonts/NotoSansKhmer-Regular.ttf" ]; then
    echo "  ⬇️  NotoSansKhmer-Regular.ttf"
    curl -sL -o fonts/NotoSansKhmer-Regular.ttf \
      "https://github.com/google/fonts/raw/main/ofl/notosanskhmer/NotoSansKhmer%5Bwdth%2Cwght%5D.ttf" \
      || curl -sL -o fonts/NotoSansKhmer-Regular.ttf \
      "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansKhmer/NotoSansKhmer-Regular.ttf"
fi

# ─── Download Noto Sans Khmer (Bold) ───────────
if [ ! -f "fonts/NotoSansKhmer-Bold.ttf" ]; then
    echo "  ⬇️  NotoSansKhmer-Bold.ttf"
    curl -sL -o fonts/NotoSansKhmer-Bold.ttf \
      "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansKhmer/NotoSansKhmer-Bold.ttf"
fi

# ─── Download Khmer OS Battambang ──────────────
if [ ! -f "fonts/KhmerOSbattambang.ttf" ]; then
    echo "  ⬇️  KhmerOSbattambang.ttf"
    curl -sL -o fonts/KhmerOSbattambang.ttf \
      "https://github.com/danhhong/khmer_fonts/raw/master/KhmerOSbattambang.ttf" \
      || echo "  ⚠️  Optional font skipped"
fi

# ─── Verify Fonts ──────────────────────────────
echo ""
echo "📋 Downloaded Fonts:"
echo "════════════════════════════════════════"
ls -lh fonts/

# ─── Install Python Dependencies ───────────────
echo ""
echo "🐍 Installing Python Packages..."
echo "════════════════════════════════════════"
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "════════════════════════════════════════"
echo "✅ Build Complete!"
echo "════════════════════════════════════════"
