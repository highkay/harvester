#!/usr/bin/env python3

"""
Web entry-point for Harvester.

Start with::

    python web_main.py

All configuration is read from environment variables — see ``web/config.py``
for the full list.
"""

from __future__ import annotations

import uvicorn

from web.config import WebSettings
from web.app import create_app


def main() -> None:
    """Build the FastAPI app and start uvicorn."""
    settings = WebSettings()
    app = create_app()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
