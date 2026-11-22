"""Punto de entrada ASGI: `uvicorn invoicebot.api.main:app`."""

from invoicebot.api.app import create_app

app = create_app()
