from contextlib import asynccontextmanager
from uuid import uuid4
from async_database import async_engine
from claim_router import router as claim_router
from scalar_fastapi import get_scalar_api_reference
from fastapi import FastAPI, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from fastapi.exceptions import RequestValidationError
from services.coverage import COVERAGE_FAILURES, resolve_coverage_by_codes
from services.claim_business import BusinessPair, ClaimBusinessEvaluator
from auth import router as auth_router, claim_access, AuthError
from services.audit import add_event, record_failure
from services.outcomes import generate_rejection
from services.diagnosis_catalog import (
    MISSING_CATALOG_CODE,
    MISSING_CATALOG_MESSAGE,
)

from schemas.claim import ClaimPayload, OperationOutcome, Issue, ClaimApproval

from database import engine, SessionLocal, get_db

# Scalar 1.69.0 leaves Test Request empty after loading #POST/process-claim.
# 1.68.0 was browser-tested with direct links, reloads, and request submission.
# Pin JavaScript independently of the scalar-fastapi Python package.
SCALAR_JS_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.68.0/dist/browser/standalone.js"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await async_engine.dispose()


app = FastAPI(
    lifespan=lifespan,
    title="Claims Rule Engine API",
    docs_url=None,
    servers=[{"url": "/"}],
)

app.include_router(auth_router)
app.include_router(claim_router)


@app.middleware("http")
async def prevent_sensitive_response_caching(request: Request, call_next):
    request.state.audit_request_id = str(uuid4())
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-ID"] = request.state.audit_request_id
    return response


@app.exception_handler(AuthError)
def authentication_error(request: Request, exc: AuthError):
    if not getattr(request.state, "login_audited", False):
        record_failure(
            request,
            "auth.authorization" if exc.status_code == 403 else "auth.authentication",
            "forbidden" if exc.status_code == 403 else "invalid_credentials",
            exc.status_code,
        )
    forbidden = exc.status_code == 403
    outcome = OperationOutcome(
        issue=[
            Issue(
                code="forbidden" if forbidden else "login",
                diagnostics=(
                    "Insufficient permissions."
                    if forbidden
                    else "Authentication failed."
                ),
            )
        ]
    )
    headers = {"Cache-Control": "no-store"}
    if not forbidden:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=exc.status_code,
        content=outcome.model_dump(mode="json", exclude_none=True),
        media_type="application/fhir+json",
        headers=headers,
    )


@app.get("/docs", include_in_schema=False)
async def custom_scalar_docs(request: Request):
    response = get_scalar_api_reference(
        content=app.openapi(),
        title=app.title,
        theme="deepSpace",
        scalar_js_url=SCALAR_JS_URL,
        # Send requests directly to the server hosting these docs.
        servers=[{"url": str(request.base_url).rstrip("/")}],
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
def validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path in {"/process-claim", "/api/v1/claims/pre-validate"}:
        reason = (
            "financial_invariant"
            if any(error["type"] == "financial_invariant" for error in exc.errors())
            else "invalid_request"
        )
        record_failure(request, "claim.validation", reason, 422)
    elif request.url.path == "/auth/login":
        record_failure(request, "auth.login", "invalid_request", 422)
    issues = []
    for error in exc.errors():
        kind = error["type"]
        code = "invalid"
        if kind == "financial_invariant":
            code = "invariant"
        elif kind == "missing":
            code = "required"
        elif kind in {"json_invalid", "extra_forbidden"}:
            code = "structure"
        # Whitelist application messages; never expose input, ctx, or arbitrary keys.
        message = {
            "json_invalid": "Request body is not valid JSON.",
            "missing": "A required field is missing.",
            "extra_forbidden": "Unknown fields are not allowed.",
        }.get(kind, "Invalid field value or type.")
        if kind in {"financial_invariant", "money_format"}:
            message = error["msg"]
        fields = [
            part
            for part in error["loc"]
            if isinstance(part, str) and part in ClaimPayload.model_fields
        ]
        if fields:
            message = f"{'.'.join(fields)}: {message}"
        issues.append(Issue(code=code, diagnostics=message))
    return JSONResponse(
        status_code=422,
        content=OperationOutcome(issue=issues).model_dump(
            mode="json", exclude_none=True
        ),
        media_type="application/fhir+json",
    )


@app.post(
    "/process-claim",
    response_model=ClaimApproval,
    dependencies=[Depends(claim_access)],
    responses={
        status: {
            "description": "Claim rejected",
            "content": {
                "application/fhir+json": {
                    "schema": OperationOutcome.model_json_schema()
                }
            },
        }
        for status in (400, 401, 403, 422, 503)
    },
)
def process_claim(claim: ClaimPayload, request: Request, db: Session = Depends(get_db)):
    # Pydantic has already enforced the financial invariants.
    try:
        result = ClaimBusinessEvaluator(
            db, coverage_resolver=resolve_coverage_by_codes
        ).evaluate(
            BusinessPair(claim.diagnosis_code, claim.service_code, claim.insurer_id)
        )
        if result.is_valid:
            add_event(db, request, "claim.validation", "success", "approved", 200)
            db.commit()
            return ClaimApproval(
                message=f"Claim approved for {result.service.display or result.service.code} with diagnosis {result.diagnosis.display or result.diagnosis.code}.",
                net_payable=claim.net_payable,
            )
        reason = result.reason
        status = 422
        code = "business-rule"
        public_reason = True
        if reason == MISSING_CATALOG_CODE:
            message, status, code = MISSING_CATALOG_MESSAGE, 503, "not-found"
        elif reason in {
            "unknown_diagnosis",
            "unknown_service",
            "ambiguous_terminology",
        }:
            message = {
                "unknown_diagnosis": "Diagnosis code is not active in the NPHIES ICD-10-AM terminology.",
                "unknown_service": "Service code is not active in the supported NPHIES service terminology.",
                "ambiguous_terminology": "Code matches multiple active terminology entries; a unique coding is required.",
            }[reason]
            status, public_reason = 400, False
        elif reason == "unknown_insurer":
            message = "Insurer ID does not identify an available insurance company."
        else:
            _, message = COVERAGE_FAILURES[result.coverage.status]
        add_event(db, request, "claim.validation", "failure", reason, status)
        db.commit()
        return generate_rejection(
            message,
            status,
            reason=reason if public_reason else None,
            code=code,
        )
    except SQLAlchemyError:
        db.rollback()
        return generate_rejection(
            "Claim validation is temporarily unavailable. Retry later.",
            503,
            code="transient",
        )
