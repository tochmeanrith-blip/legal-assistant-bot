#!/usr/bin/env bash
# Cambodia Legal Bot v17.5 - Build Script for Render Free Tier
set -o errexit

echo "════════════════════════════════════════════"
echo "🚀 Cambodia Legal Bot v17.5 - Building..."
echo "════════════════════════════════════════════"

# ─── Update apt package lists ─────────────────────
echo ""
echo "📦 Step 1/4: Updating apt sources..."
echo "────────────────────────────────────────────"
apt-get update -qq 2>&1 | tail -5 || echo "⚠️ apt-update warning (continuing...)"

# ─── Install WeasyPrint System Dependencies ───────
echo ""
echo "📦 Step 2/4: Installing WeasyPrint dependencies..."
echo "────────────────────────────────────────────"
apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fontconfig \
    2>&1 | tail -3 || echo "⚠️ Some packages may have failed"

# ─── Install Khmer Fonts ──────────────────────────
echo ""
echo "🔤 Step 3/4: Installing Khmer Fonts..."
echo "────────────────────────────────────────────"
apt-get install -y --no-install-recommends \
    fonts-noto \
    fonts-noto-core \
    fonts-khmeros \
    fonts-khmeros-core \
    2>&1 | tail -3 || echo "⚠️ Some fonts may have failed"

# ─── Download Fallback Fonts (in case apt fails) ──
echo ""
echo "📥 Downloading backup fonts..."
mkdir -p fonts

if [ ! -f "fonts/NotoSansKhmer-Regular.ttf" ]; then
    echo "  ⬇️  NotoSansKhmer-Regular.ttf"
    curl -sL --fail -o fonts/NotoSansKhmer-Regular.ttf \
      "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansKhmer/NotoSansKhmer-Regular.ttf" \
      && echo "    ✅ Downloaded" \
      || echo "    ⚠️ Download failed"
fi

if [ ! -f "fonts/NotoSansKhmer-Bold.ttf" ]; then
    echo "  ⬇️  NotoSansKhmer-Bold.ttf"
    curl -sL --fail -o fonts/NotoSansKhmer-Bold.ttf \
      "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansKhmer/NotoSansKhmer-Bold.ttf" \
      && echo "    ✅ Downloaded" \
      || echo "    ⚠️ Download failed"
fi

# ─── Refresh Font Cache ───────────────────────────
echo ""
echo "🔄 Refreshing font cache..."
fc-cache -f 2>/dev/null || echo "⚠️ fc-cache warning"

# ─── Verify Fonts ─────────────────────────────────
echo ""
echo "📋 Available Khmer Fonts:"
echo "────────────────────────────────────────────"
fc-list 2>/dev/null | grep -i khmer | head -10 || echo "⚠️ No system Khmer fonts detected"
echo ""
echo "📁 Local fonts folder:"
ls -lh fonts/ 2>/dev/null || echo "No local fonts"

# ─── Install Python Packages ──────────────────────
echo ""
echo "🐍 Step 4/4: Installing Python Packages..."
echo "────────────────────────────────────────────"
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "════════════════════════════════════════════"
echo "✅ Build Complete! Starting bot..."
echo "════════════════════════════════════════════"
