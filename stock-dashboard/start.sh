#!/bin/bash
cd "$(dirname "$0")"
exec python3 stock_dashboard.py --port "${PORT:-8051}"
