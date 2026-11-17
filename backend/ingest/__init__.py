"""Production ingest: async venue adapters, a per-venue rate limiter, and the background poller.

Parsing is shared with `engine.dev_fetch`; only the HTTP layer is different (async httpx).
"""
