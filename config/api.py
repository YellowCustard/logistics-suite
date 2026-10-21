from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ObjectDoesNotExist
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.masterdata.api import router as masterdata_router
from apps.oms.api import router as oms_router
from apps.platform.api import auth_router, health_router, platform_router
from apps.platform.apiutils import Problem
from apps.platform.auth import BearerTokenAuth, PrincipalSessionAuth
from apps.wms.api import router as wms_router

api = NinjaAPI(
    title="Logistics Suite API",
    version="1.0.0",
    urls_namespace="api-v1",
    auth=[BearerTokenAuth(), PrincipalSessionAuth()],
    docs_decorator=staff_member_required,  # schema and docs only for logged-in staff
    description="All mutating requests require an Idempotency-Key header. Errors are RFC 9457 problems.",
)


def _problem(request, status: int, body: dict):
    return api.create_response(request, body, status=status)


@api.exception_handler(Problem)
def on_problem(request, exc: Problem):
    response = _problem(request, exc.status, exc.body())
    response["Content-Type"] = "application/problem+json"
    return response


@api.exception_handler(ValidationError)
def on_validation(request, exc: ValidationError):
    errors = [{"field": ".".join(str(p) for p in e.get("loc", [])), "message": e.get("msg", "")} for e in exc.errors]
    return on_problem(request, Problem(422, "Validation failed", errors=errors))


@api.exception_handler(AuthenticationError)
def on_auth(request, exc):
    return on_problem(request, Problem(401, "Unauthorized", "Send a Bearer token or a valid session."))


@api.exception_handler(HttpError)
def on_http(request, exc: HttpError):
    return on_problem(request, Problem(exc.status_code, str(exc) or "Error"))


@api.exception_handler(ObjectDoesNotExist)
def on_missing(request, exc):
    return on_problem(request, Problem(404, "Not found"))


api.add_router("/auth", auth_router)
api.add_router("/", health_router)
api.add_router("/", platform_router)
api.add_router("/", masterdata_router)
api.add_router("/", oms_router)
api.add_router("/", wms_router)
