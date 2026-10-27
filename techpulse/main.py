"""ASGI entrypoint: `uvicorn techpulse.main:app`."""

import logging

from techpulse.api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

app = create_app()
