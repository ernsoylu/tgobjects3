"""FastAPI routing/dispatch skeleton for the S3-compatible HTTP surface.

This module is the single entry point for the S3 API. Its sole responsibility is
*dispatch*: given an inbound HTTP request, decide which S3 operation it names and
hand it to the matching operation handler. The operation handlers themselves
(``put_object``, ``get_object``, bucket operations, multipart, listing, batch
delete, …) are implemented by sibling tasks and plug in through the
:class:`S3Handlers` registry; this file does not implement any operation body.

Why a hand-rolled dispatcher
----------------------------
The S3 wire protocol does not map cleanly onto REST routing frameworks. A single
``(method, path-shape)`` pair fans out to many operations distinguished only by
the *presence* of a query parameter — ``POST /{bucket}/{key}?uploads`` creates a
multipart upload, ``PUT /{bucket}/{key}?partNumber=1&uploadId=…`` uploads a part,
``GET /{bucket}?location`` reads a bucket's region, and so on (design
"S3 API Layer": "routes dispatched by HTTP method and presence of query
parameters (e.g., ``?uploads``, ``?partNumber``)"). We therefore register a
small number of catch-all FastAPI routes keyed only on method and path shape,
and resolve the concrete operation ourselves in :func:`classify_operation`,
which is a pure function and directly unit-testable.

Addressing styles
-----------------
Both S3 bucket-addressing styles are supported (requirement 4.1 — the gateway
exposes the standard bucket operations, which clients reach via either style):

* **path-style** — ``https://endpoint/{bucket}/{key}``; the bucket is the first
  path segment.
* **virtual-hosted-style** — ``https://{bucket}.endpoint/{key}``; the bucket is a
  label of the ``Host`` header and the path carries only the key.

:func:`resolve_addressing` normalises both into a :class:`ResolvedTarget`
(``bucket``/``key``) so the rest of the dispatcher is addressing-agnostic.

Authentication boundary
------------------------
Every request is authenticated with SigV4 *before* its body is processed
(requirement 6.1: "verify the SigV4 signature before processing the request
body"; requirement 7.1/7.2 select header vs presigned mode). The router builds
the transport-agnostic :class:`~gateway.app.s3.sigv4.SignedRequest` snapshot from
the FastAPI request and runs the matching verifier. Authentication is wired as a
seam (:class:`Authenticator`) so the operation-dispatch logic stays independent
of the credential store, which lands in a later task.

Error responses
---------------
Every failure is rendered as the standard S3 error XML document via
:func:`gateway.app.s3.xml_codecs.serialise_error`, with the HTTP status taken
from the design's "Request Errors" table. Operations that are not yet
implemented dispatch to :func:`_not_implemented`, which returns a
``501 NotImplemented`` error document; the sibling tasks replace those stubs by
registering real handlers on :class:`S3Handlers`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol, runtime_checkable

from fastapi import APIRouter, Request, Response

from gateway.app.config import Settings
from gateway.app.s3 import sigv4, xml_codecs

__all__ = [
    "S3Operation",
    "ResolvedTarget",
    "S3HandlerContext",
    "S3Handler",
    "S3Handlers",
    "Authenticator",
    "SigV4Authenticator",
    "classify_operation",
    "resolve_addressing",
    "build_signed_request",
    "error_response",
    "create_s3_router",
]


# ---------------------------------------------------------------------------
# Operation taxonomy
# ---------------------------------------------------------------------------


class S3Operation(Enum):
    """The S3 operation an inbound request names.

    :func:`classify_operation` maps a ``(method, path-shape, query-keys)`` triple
    to exactly one member. Each member corresponds to a handler slot on
    :class:`S3Handlers` filled in by a sibling task; keeping the taxonomy in one
    enum means dispatch, the handler registry, and tests all agree on the
    operation set.
    """

    # Service root ("/")
    LIST_BUCKETS = auto()

    # Bucket-level ("/{bucket}")
    CREATE_BUCKET = auto()
    DELETE_BUCKET = auto()
    HEAD_BUCKET = auto()
    GET_BUCKET_LOCATION = auto()
    LIST_OBJECTS_V2 = auto()
    LIST_OBJECTS_V1 = auto()
    LIST_MULTIPART_UPLOADS = auto()
    DELETE_OBJECTS = auto()

    # Object-level ("/{bucket}/{key}")
    PUT_OBJECT = auto()
    GET_OBJECT = auto()
    HEAD_OBJECT = auto()
    DELETE_OBJECT = auto()
    CREATE_MULTIPART_UPLOAD = auto()
    COMPLETE_MULTIPART_UPLOAD = auto()
    ABORT_MULTIPART_UPLOAD = auto()
    UPLOAD_PART = auto()
    LIST_PARTS = auto()


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedTarget:
    """The bucket and key an inbound request addresses.

    A frozen value object (per the project's "typed value objects at
    boundaries" convention) produced by :func:`resolve_addressing` after
    collapsing virtual-hosted- and path-style addressing into one shape.

    Attributes:
        bucket: The target bucket name, or ``None`` for a service-root request
            (``GET /`` → ListBuckets).
        key: The target object key, or ``None`` for a bucket-level request. The
            key is the raw, percent-decoded remainder of the path and may itself
            contain ``/`` separators.
    """

    bucket: str | None
    key: str | None


def resolve_addressing(
    *, host: str | None, path: str, service_host: str
) -> ResolvedTarget:
    """Collapse virtual-hosted- and path-style addressing to a :class:`ResolvedTarget`.

    Supports both S3 addressing styles (requirement 4.1):

    * **virtual-hosted-style** — when ``host`` is a strict subdomain of
      ``service_host`` (``{bucket}.{service_host}``), the leading label is the
      bucket and the entire path (minus its leading slash) is the key.
    * **path-style** — otherwise the first path segment is the bucket and the
      remainder is the key.

    Args:
        host: The request ``Host`` header value (without any port), or ``None``.
        path: The request URL path, percent-decoded once, beginning with ``/``.
        service_host: The gateway's own endpoint host, used to detect a
            virtual-hosted bucket label. Compared case-insensitively.

    Returns:
        A :class:`ResolvedTarget`; ``bucket`` is ``None`` only for the service
        root, and ``key`` is ``None`` when no key segment is present.
    """
    bucket_label = _virtual_host_bucket(host, service_host)
    trimmed = path[1:] if path.startswith("/") else path

    if bucket_label is not None:
        # Virtual-hosted-style: the whole path is the key.
        key = trimmed or None
        return ResolvedTarget(bucket=bucket_label, key=key)

    # Path-style: first segment is the bucket, the rest is the key.
    if not trimmed:
        return ResolvedTarget(bucket=None, key=None)
    bucket, slash, key = trimmed.partition("/")
    if not bucket:
        return ResolvedTarget(bucket=None, key=None)
    return ResolvedTarget(bucket=bucket, key=(key if slash and key else None))


def _virtual_host_bucket(host: str | None, service_host: str) -> str | None:
    """Return the bucket label if ``host`` is virtual-hosted, else ``None``.

    ``host`` is virtual-hosted when it is ``{label}.{service_host}`` for a
    non-empty ``label``. A bare ``service_host`` (path-style) or an unrelated
    host yields ``None``. The port, if any, must already be stripped by the
    caller. The comparison is case-insensitive, as DNS names are.
    """
    if not host:
        return None
    host_l = host.lower()
    service_l = service_host.lower()
    suffix = "." + service_l
    if host_l == service_l or not host_l.endswith(suffix):
        return None
    label = host_l[: -len(suffix)]
    return label or None


# ---------------------------------------------------------------------------
# Operation classification (pure)
# ---------------------------------------------------------------------------

#: Query keys that select a non-default bucket-level or object-level operation.
#: Membership — not value — is what matters for most of these, matching the S3
#: convention (``?uploads``, ``?location``, ``?delete`` are valueless flags).
_UPLOADS = "uploads"
_UPLOAD_ID = "uploadId"
_PART_NUMBER = "partNumber"
_DELETE = "delete"
_LOCATION = "location"
_LIST_TYPE = "list-type"


class UnsupportedOperationError(Exception):
    """The request's ``(method, path, query)`` triple names no known operation.

    Raised by :func:`classify_operation` for method/shape combinations S3 does
    not define (e.g. ``PATCH /{bucket}``). The router maps it to a
    ``405 MethodNotAllowed`` error document rather than letting it escape as a
    500.
    """


def classify_operation(
    *, method: str, target: ResolvedTarget, query: Mapping[str, str]
) -> S3Operation:
    """Resolve the concrete :class:`S3Operation` for a request.

    Pure dispatch logic — no I/O — so it is exhaustively unit-testable. The
    decision is driven by the HTTP method, the path shape implied by ``target``
    (service root vs bucket-level vs object-level), and the presence of the
    selector query keys S3 uses to multiplex operations onto a single method +
    path (design "S3 API Layer").

    Args:
        method: The HTTP method, any case.
        target: The resolved bucket/key for the request.
        query: The request's query parameters (keys are what matter here).

    Returns:
        The single operation the request names.

    Raises:
        UnsupportedOperationError: If the triple matches no S3 operation.
    """
    verb = method.upper()

    # Service root: only GET / (ListBuckets).
    if target.bucket is None:
        if verb == "GET":
            return S3Operation.LIST_BUCKETS
        raise UnsupportedOperationError(f"{verb} / is not a valid S3 operation")

    if target.key is None:
        return _classify_bucket_operation(verb, query)
    return _classify_object_operation(verb, query)


def _classify_bucket_operation(
    verb: str, query: Mapping[str, str]
) -> S3Operation:
    """Classify a bucket-level ("/{bucket}") request.

    Handles the ListBuckets-adjacent bucket verbs plus the query-selected
    bucket sub-resources (``?location``, ``?uploads``, ``?delete``) and the two
    listing dialects (requirements 4.1, 5.1).
    """
    if verb == "GET":
        if _LOCATION in query:
            return S3Operation.GET_BUCKET_LOCATION
        if _UPLOADS in query:
            return S3Operation.LIST_MULTIPART_UPLOADS
        if query.get(_LIST_TYPE) == "2":
            return S3Operation.LIST_OBJECTS_V2
        return S3Operation.LIST_OBJECTS_V1
    if verb == "HEAD":
        return S3Operation.HEAD_BUCKET
    if verb == "PUT":
        return S3Operation.CREATE_BUCKET
    if verb == "DELETE":
        return S3Operation.DELETE_BUCKET
    if verb == "POST":
        if _DELETE in query:
            return S3Operation.DELETE_OBJECTS
        raise UnsupportedOperationError("POST /{bucket} requires ?delete")
    raise UnsupportedOperationError(f"{verb} /{{bucket}} is not supported")


def _classify_object_operation(
    verb: str, query: Mapping[str, str]
) -> S3Operation:
    """Classify an object-level ("/{bucket}/{key}") request.

    Resolves the plain object verbs and the multipart sub-resources selected by
    ``?uploads``, ``?uploadId`` and ``?partNumber`` (requirement 6.1 for
    PutObject; multipart operations are implemented by sibling tasks).
    """
    has_upload_id = _UPLOAD_ID in query
    has_part_number = _PART_NUMBER in query

    if verb == "GET":
        if has_upload_id:
            return S3Operation.LIST_PARTS
        return S3Operation.GET_OBJECT
    if verb == "HEAD":
        return S3Operation.HEAD_OBJECT
    if verb == "PUT":
        if has_upload_id and has_part_number:
            return S3Operation.UPLOAD_PART
        return S3Operation.PUT_OBJECT
    if verb == "POST":
        if _UPLOADS in query:
            return S3Operation.CREATE_MULTIPART_UPLOAD
        if has_upload_id:
            return S3Operation.COMPLETE_MULTIPART_UPLOAD
        raise UnsupportedOperationError(
            "POST /{bucket}/{key} requires ?uploads or ?uploadId"
        )
    if verb == "DELETE":
        if has_upload_id:
            return S3Operation.ABORT_MULTIPART_UPLOAD
        return S3Operation.DELETE_OBJECT
    raise UnsupportedOperationError(f"{verb} /{{bucket}}/{{key}} is not supported")


# ---------------------------------------------------------------------------
# Handler contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class S3HandlerContext:
    """Everything an operation handler needs, assembled by the router.

    A frozen value object handed to each :class:`S3Handler`. Bundling the
    resolved target, the authenticated identity and the raw request keeps
    handler signatures stable as sibling tasks fill in the operation bodies.

    Attributes:
        request: The live FastAPI request, for streaming the body and reading
            headers the handler needs beyond those used for signing.
        operation: The classified operation, so a shared handler can branch if
            it serves more than one slot.
        target: The resolved bucket/key the request addresses.
        auth: The successful SigV4 verification result (requirement 6.1 — auth
            runs before the handler sees the body).
        settings: The gateway configuration, consumed per the "config is
            centralized" convention; handlers never read the environment.
    """

    request: Request
    operation: S3Operation
    target: ResolvedTarget
    auth: sigv4.AuthResult
    settings: Settings


@runtime_checkable
class S3Handler(Protocol):
    """One S3 operation handler.

    Sibling tasks implement concrete handlers (PutObject, GetObject, bucket
    operations, multipart, …) and register them on :class:`S3Handlers`. The
    router supplies a fully-populated :class:`S3HandlerContext` and expects a
    ready-to-send :class:`fastapi.Response`.
    """

    async def __call__(self, context: S3HandlerContext) -> Response:
        """Execute the operation and return its HTTP response."""
        ...


#: A handler callable; the structural :class:`S3Handler` protocol spelled as a
#: plain alias so free functions (not just classes) register cleanly.
S3HandlerCallable = Callable[[S3HandlerContext], Awaitable[Response]]


class S3Handlers:
    """Mutable registry mapping each :class:`S3Operation` to its handler.

    The router owns one instance. Sibling tasks call :meth:`register` to plug a
    real handler into a slot; any slot left unregistered falls back to
    :func:`_not_implemented`, so the router is runnable end-to-end from the
    first task and each later task lights up its own operation without touching
    the dispatcher.
    """

    def __init__(self) -> None:
        """Create an empty registry with every slot unfilled."""
        self._handlers: dict[S3Operation, S3HandlerCallable] = {}

    def register(
        self, operation: S3Operation, handler: S3HandlerCallable
    ) -> None:
        """Bind ``handler`` to ``operation``, replacing any previous binding."""
        self._handlers[operation] = handler

    def resolve(self, operation: S3Operation) -> S3HandlerCallable:
        """Return the handler for ``operation``, or the not-implemented stub.

        Returning a working stub rather than raising keeps an unimplemented
        operation a well-formed ``501`` to the client instead of a server
        crash, which is the behaviour the later tasks replace.
        """
        return self._handlers.get(operation, _not_implemented)


async def _not_implemented(context: S3HandlerContext) -> Response:
    """Fallback handler for operations no sibling task has registered yet.

    Returns the standard S3 error document with code ``NotImplemented`` and
    HTTP 501, naming the operation so it is obvious which handler is still a
    stub. Sibling tasks replace this by registering a real handler for the slot.
    """
    return error_response(
        code="NotImplemented",
        message=(
            f"operation {context.operation.name} is not implemented yet"
        ),
        status_code=501,
        resource=_resource_for(context.target),
    )


# ---------------------------------------------------------------------------
# Authentication seam
# ---------------------------------------------------------------------------


@runtime_checkable
class Authenticator(Protocol):
    """Authenticates a request snapshot, returning the verified identity.

    A seam over :mod:`gateway.app.s3.sigv4` so the dispatcher does not depend on
    the credential store directly (which a later task supplies). Implementations
    raise :class:`gateway.app.s3.sigv4.SigV4Error` subclasses on failure, which
    the router renders as the appropriate 400/403 error document.
    """

    async def authenticate(self, request: sigv4.SignedRequest) -> sigv4.AuthResult:
        """Verify ``request`` and return the authenticated result."""
        ...


class SigV4Authenticator:
    """Default :class:`Authenticator` dispatching to the SigV4 verifiers.

    Chooses header vs presigned verification by the same rule the protocol uses
    (requirements 7.1, 7.2): a request carrying an ``X-Amz-Signature`` query
    parameter is presigned, otherwise it is header-authenticated. The
    :class:`~gateway.app.s3.sigv4.SecretProvider` that resolves access-key ids to
    stored secrets is injected, so the credential store lands in a later task
    without changing this class.
    """

    def __init__(self, secret_provider: sigv4.SecretProvider) -> None:
        """Store the secret provider used for every verification."""
        self._secret_provider = secret_provider

    async def authenticate(
        self, request: sigv4.SignedRequest
    ) -> sigv4.AuthResult:
        """Verify ``request`` with the mode its parameters select.

        Runs before any body processing (requirement 6.1). Presigned mode is
        selected by the presence of ``X-Amz-Signature`` in the query string
        (requirement 7.2); otherwise header authentication is used
        (requirement 7.1).
        """
        if "X-Amz-Signature" in request.query:
            return await sigv4.verify_presigned_auth(request, self._secret_provider)
        return await sigv4.verify_header_auth(request, self._secret_provider)


def build_signed_request(request: Request) -> sigv4.SignedRequest:
    """Snapshot a FastAPI request into the SigV4 :class:`~gateway.app.s3.sigv4.SignedRequest`.

    Decouples signature verification from the ASGI request object (per sigv4's
    "transport-agnostic value object" design). Header names are lower-cased to
    match the canonicalisation rules, and the single-valued view of the query
    string is used, which is what the SigV4 canonical query string expects.

    Args:
        request: The live FastAPI request.

    Returns:
        A frozen snapshot carrying the method, path, query, headers and declared
        payload-hash mode the verifiers depend on.
    """
    headers = {k.lower(): v for k, v in request.headers.items()}
    query = dict(request.query_params)
    return sigv4.SignedRequest(
        method=request.method,
        path=request.url.path,
        query=query,
        headers=headers,
        content_sha256=headers.get("x-amz-content-sha256"),
    )


# ---------------------------------------------------------------------------
# Error rendering
# ---------------------------------------------------------------------------

#: Maps a recoverable SigV4 failure to its ``(S3 code, HTTP status)`` per the
#: design's "Request Errors" table.
_SIGV4_ERROR_MAP: dict[type[sigv4.SigV4Error], tuple[str, int]] = {
    sigv4.SignatureDoesNotMatch: ("SignatureDoesNotMatch", 403),
    sigv4.RequestTimeTooSkewed: ("RequestTimeTooSkewed", 403),
    sigv4.MalformedAuthError: ("AuthorizationHeaderMalformed", 400),
    sigv4.SecretVerificationUnavailableError: ("InternalError", 500),
}


def error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    resource: str | None = None,
    request_id: str | None = None,
) -> Response:
    """Build a FastAPI :class:`Response` carrying the standard S3 error XML body.

    Centralises error framing so every failure path emits the same
    ``application/xml`` document produced by
    :func:`gateway.app.s3.xml_codecs.serialise_error`, with the HTTP status from
    the design's "Request Errors" table.

    Args:
        code: The S3 error code (e.g. ``NoSuchBucket``).
        message: A human-readable description.
        status_code: The HTTP status to send.
        resource: The bucket/key the request targeted, when known.
        request_id: The gateway request id, echoed for correlation.

    Returns:
        A ready-to-send XML error response.
    """
    body = xml_codecs.serialise_error(
        xml_codecs.Error(
            code=code,
            message=message,
            resource=resource,
            request_id=request_id,
        )
    )
    return Response(
        content=body,
        status_code=status_code,
        media_type="application/xml",
    )


def _sigv4_error_response(
    error: sigv4.SigV4Error, target: ResolvedTarget
) -> Response:
    """Render a SigV4 failure as its mapped S3 error document.

    Unknown subclasses fall back to ``403 AccessDenied`` rather than leaking a
    500, keeping authentication failures uniformly client-visible.
    """
    code, status = _SIGV4_ERROR_MAP.get(type(error), ("AccessDenied", 403))
    return error_response(
        code=code,
        message=str(error) or code,
        status_code=status,
        resource=_resource_for(target),
    )


def _resource_for(target: ResolvedTarget) -> str | None:
    """Format the ``Resource`` element value for an error document.

    Produces ``/{bucket}/{key}``, ``/{bucket}`` or ``None`` to mirror the path
    shape the request addressed.
    """
    if target.bucket is None:
        return None
    if target.key is None:
        return f"/{target.bucket}"
    return f"/{target.bucket}/{target.key}"


def _host_without_port(host: str | None) -> str | None:
    """Strip a trailing ``:port`` from a ``Host`` header value.

    IPv6 literals are bracketed (``[::1]:9000``); the port is only the suffix
    after the last colon when that colon is outside any bracket, so a bracketed
    literal without a port is returned unchanged.
    """
    if host is None:
        return None
    if host.startswith("["):
        close = host.find("]")
        if close == -1:
            return host
        return host[: close + 1]
    base, sep, _ = host.rpartition(":")
    return base if sep else host


# ---------------------------------------------------------------------------
# Router assembly
# ---------------------------------------------------------------------------


def create_s3_router(
    *,
    settings: Settings,
    handlers: S3Handlers,
    authenticator: Authenticator,
    service_host: str,
) -> APIRouter:
    """Build the S3 :class:`fastapi.APIRouter` mounted at ``/`` on the S3 port.

    Registers a single catch-all route per HTTP method; every request flows
    through :func:`_dispatch`, which authenticates it (requirement 6.1),
    resolves addressing (requirement 4.1), classifies the operation, and hands
    off to the registered handler (or the not-implemented stub). The router is
    mounted under ``/`` on the S3 port by the application wiring task; this
    function only builds and returns it.

    Args:
        settings: The gateway configuration passed to every handler.
        handlers: The operation registry; sibling tasks populate its slots.
        authenticator: The SigV4 authentication seam.
        service_host: The gateway's own endpoint host, used to detect
            virtual-hosted bucket labels.

    Returns:
        The configured router, ready to mount.
    """
    router = APIRouter()

    async def dispatch(request: Request) -> Response:
        return await _dispatch(
            request,
            settings=settings,
            handlers=handlers,
            authenticator=authenticator,
            service_host=service_host,
        )

    # A catch-all per method keyed only on path shape; the concrete operation is
    # resolved in _dispatch, because S3 multiplexes many operations onto the
    # same (method, path) via query parameters.
    for method in ("GET", "PUT", "POST", "DELETE", "HEAD"):
        router.add_api_route(
            "/{path:path}",
            dispatch,
            methods=[method],
            include_in_schema=False,
        )

    return router


async def _dispatch(
    request: Request,
    *,
    settings: Settings,
    handlers: S3Handlers,
    authenticator: Authenticator,
    service_host: str,
) -> Response:
    """Authenticate, classify, and route a single request to its handler.

    The dispatch pipeline, in order:

    1. Build the SigV4 snapshot and authenticate *before* touching the body
       (requirement 6.1); auth failures become 400/403 error documents.
    2. Resolve virtual-hosted/path-style addressing (requirement 4.1).
    3. Classify the operation from method + path shape + query keys; an
       unknown combination becomes ``405 MethodNotAllowed``.
    4. Hand the populated context to the registered handler, or to the
       not-implemented stub for slots a sibling task has not filled yet.
    """
    signed = build_signed_request(request)
    try:
        auth = await authenticator.authenticate(signed)
    except sigv4.SigV4Error as exc:
        target = resolve_addressing(
            host=_host_without_port(request.headers.get("host")),
            path=request.url.path,
            service_host=service_host,
        )
        return _sigv4_error_response(exc, target)

    target = resolve_addressing(
        host=_host_without_port(request.headers.get("host")),
        path=request.url.path,
        service_host=service_host,
    )

    try:
        operation = classify_operation(
            method=request.method,
            target=target,
            query=dict(request.query_params),
        )
    except UnsupportedOperationError as exc:
        return error_response(
            code="MethodNotAllowed",
            message=str(exc),
            status_code=405,
            resource=_resource_for(target),
        )

    handler = handlers.resolve(operation)
    context = S3HandlerContext(
        request=request,
        operation=operation,
        target=target,
        auth=auth,
        settings=settings,
    )
    return await handler(context)
