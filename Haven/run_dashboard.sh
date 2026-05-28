#!/bin/bash
set -e

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ -z "$Grok_XAI_API_KEY" ] && [ -z "$GROQ_API_KEY" ]; then
  echo "ERROR: neither Grok_XAI_API_KEY nor GROQ_API_KEY is set."
  echo "Run: export Grok_XAI_API_KEY=\"your_api_key_here\""
  exit 1
fi

python3 app.py
