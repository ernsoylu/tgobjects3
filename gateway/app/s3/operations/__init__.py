"""Concrete S3 operation handlers.

Each module in this package implements one (or a small, related group of) S3
operation handler conforming to the :class:`gateway.app.s3.router.S3Handler`
protocol: an async callable that takes a
:class:`gateway.app.s3.router.S3HandlerContext` and returns a FastAPI
``Response``. The router (``gateway/app/s3/router.py``) classifies each inbound
request to an :class:`gateway.app.s3.router.S3Operation` and dispatches to the
handler registered for that slot; the handlers here fill those slots.

Authentication (SigV4) is already performed by the router before a handler is
invoked, so handlers receive the authenticated identity on the context and never
re-verify signatures.
"""

from __future__ import annotations
