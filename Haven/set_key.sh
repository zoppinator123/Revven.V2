#!/bin/bash
# Run this once to save your Gemini API key.
# Your key will NOT appear on screen as you type it.
echo ""
echo AIzaSyCTR9be4ukbsXAYrH-n-wbb1j6i5cxQd4w:"
read -rs GEMINI_API_KEY
echo ""

if [ -z AIzaSyCTR9be4ukbsXAYrH-n-wbb1j6i5cxQd4w ]; then
  echo "ERROR: No key entered."
  exit 1
fi

# Write to ~/.zshrc
grep -v AIzaSyCTR9be4ukbsXAYrH-n-wbb1j6i5cxQd4w ~/.zshrc > /tmp/zshrc_tmp && mv /tmp/zshrc_tmp ~/.zshrc
echo "export AIzaSyCTR9be4ukbsXAYrH-n-wbb1j6i5cxQd4w=\AIzaSyCTR9be4ukbsXAYrH-n-wbb1j6i5cxQd4w" >> ~/.zshrc

# Load immediately
export GEMINI_API_KEY=AIzaSyCTR9be4ukbsXAYrH-n-wbb1j6i5cxQd4w

echo "Key saved to ~/.zshrc and loaded into this session."
echo "Key starts with: ${GEMINI_API_KEY:0:8}..."
echo ""
echo "Now run:  python3 /Users/noelineramos/Documents/MPP/app.py"
