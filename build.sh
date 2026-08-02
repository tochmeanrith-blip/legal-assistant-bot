#!/usr/bin/env bash
set -o errexit

echo "════════════════════════════════════════"
echo "🚀 Cambodia Legal Bot v17.5"
echo "   HTML Preview + PDF Save"
echo "════════════════════════════════════════"

pip install --upgrade pip
pip install -r requirements.txt

mkdir -p templates static previews

echo "✅ Build Complete!"
