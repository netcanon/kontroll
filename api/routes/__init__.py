"""API route groups — one router module per domain. Adding a domain = adding a file here AND registering it in
api/main.py (its import and the tuple in `create_app()`); tests/integration/test_api.py fails for a module that
defines a router and is not registered.
"""
