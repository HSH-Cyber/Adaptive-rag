"""ASGI entrypoint exposing the module-level FastAPI app."""

from app.main import create_app

#告诉服务器隐形哪个web应用
app = create_app()
