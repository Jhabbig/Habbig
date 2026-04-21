"""Starlette/FastAPI middleware modules for the gateway.

Each submodule exposes either a BaseHTTPMiddleware subclass (for plain
request/response hooks) or a factory that returns one. Registered from
server.py with explicit ordering — see the comments there.
"""
