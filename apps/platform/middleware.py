import uuid

from .context import Actor, actor_var, correlation_id_var, request_id_var
from .ids import uuid7

ANONYMOUS = Actor(kind="anonymous", id=None, label="anonymous")


def _parse_uuid(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


class RequestContextMiddleware:
    """Assigns a request id (honouring a valid inbound X-Request-ID) and echoes it back."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = _parse_uuid(request.headers.get("X-Request-ID")) or uuid7()
        request.request_id = request_id
        # Every request starts from a clean context; WSGI threads are reused across requests.
        tokens = (request_id_var.set(request_id), correlation_id_var.set(request_id), actor_var.set(ANONYMOUS))
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(tokens[0])
            correlation_id_var.reset(tokens[1])
            actor_var.reset(tokens[2])
        response["X-Request-ID"] = str(request_id)
        return response


class ActorContextMiddleware:
    """Sets the actor for session-authenticated users. API token auth sets it in the auth class."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        token = None
        if user is not None and user.is_authenticated:
            token = actor_var.set(Actor(kind="user", id=user.pk, label=user.email))
        try:
            return self.get_response(request)
        finally:
            if token is not None:
                actor_var.reset(token)
