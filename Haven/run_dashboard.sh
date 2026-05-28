#!/bin/bash
set -e

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ -z "$GROQ_API_KEY" ]; then
  echo "ERROR: GROQ_API_KEY is not set."
  echo "Run: export GROQ_API_KEY=\"your_groq_api_key_here\""
  exit 1
fi

python3 app.py
