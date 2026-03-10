#!/bin/bash
# Quick start for local use
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "Setting up virtual environment..."
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
fi

echo ""
echo "============================================"
echo "  📖 Social Reader"
echo "  http://localhost:8000"
echo "  Instructor code: $(grep INSTRUCTOR_CODE .env | cut -d= -f2)"
echo "============================================"
echo ""

venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
