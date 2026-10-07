#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python -m uvicorn webapp.main:app --host 127.0.0.1 --port "${PORT:-5604}" --reload