#!/bin/sh
# Harvester Web Mode Docker Entrypoint
# Generates ENCRYPTION_KEY if not set, then starts the FastAPI server.
# This file coexists with entrypoint.sh (CLI mode) — do not modify the original.

set -e

# --- ENCRYPTION_KEY: generate if not provided ---
if [ -z "$ENCRYPTION_KEY" ]; then
    if command -v openssl >/dev/null 2>&1; then
        ENCRYPTION_KEY=$(openssl rand -hex 32)
    else
        # Fallback: use python if openssl is unavailable (e.g. minimal slim image)
        ENCRYPTION_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    fi
    export ENCRYPTION_KEY
    echo ""
    echo "============================================================"
    echo "  ENCRYPTION_KEY was auto-generated."
    echo "  Record this value to persist across container restarts:"
    echo ""
    echo "  $ENCRYPTION_KEY"
    echo ""
    echo "  Set ENCRYPTION_KEY in your .env / docker-compose.yml to"
    echo "  avoid regeneration on every startup."
    echo "============================================================"
    echo ""
fi

# --- Start the web server ---
exec python web_main.py
