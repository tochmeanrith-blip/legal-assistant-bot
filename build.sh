#!/usr/bin/env bash
# build.sh - Install system dependencies for WeasyPrint + Khmer fonts

set -o errexit

echo "📦 Installing system dependencies..."
apt-get update
apt-get install -y \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    fonts-khmeros \
    fonts-khmeros-core \
    fonts-noto \
    fonts-noto-cjk

echo "📦 Installing Python packages..."
pip install --upgrade pip
pip install -r requirements.txt

echo "✅ Build complete!"
