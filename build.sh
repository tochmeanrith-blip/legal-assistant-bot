#!/usr/bin/env bash
# Exit on error
set -o errexit

echo "════════════════════════════════════"
echo "📦 Installing System Dependencies..."
echo "════════════════════════════════════"

# Update apt
apt-get update -qq

# Install WeasyPrint dependencies + Khmer fonts
apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-noto \
    fonts-noto-core \
    fonts-khmeros \
    fonts-khmeros-core \
    fontconfig

echo "════════════════════════════════════"
echo "🔤 Refreshing Font Cache..."
echo "════════════════════════════════════"

# Refresh font cache
fc-cache -f -v

# Verify Khmer fonts installed
echo "📋 Available Khmer Fonts:"
fc-list | grep -i khmer || echo "⚠️ No Khmer fonts found!"

echo "════════════════════════════════════"
echo "🐍 Installing Python Packages..."
echo "════════════════════════════════════"

# Install Python packages
pip install --upgrade pip
pip install -r requirements.txt

echo "════════════════════════════════════"
echo "✅ Build Complete!"
echo "════════════════════════════════════"
