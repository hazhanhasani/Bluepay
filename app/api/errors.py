from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def request_id_for(request: Request) -> str:
    return getattr(request.state, 'request_id', 'req_unknown')


def error_payload(
    request: Request,
    *,
    code: str,
    message: str,
    field: str | None = None,
    details: Any = None,
) -> dict[str, Any]:
    return {
        'success': False,
        'error': {
            'code': code,
            'message': message,
            'field': field,
            'details': details,
        },
        'request_id': request_id_for(request),
        'timestamp': _timestamp(),
    }


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    field: str | None = None,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(
            request,
            code=code,
            message=message,
            field=field,
            details=details,
        ),
        headers=headers,
    )


def default_error_code(status_code: int) -> str:
    return {
        400: 'BAD_REQUEST',
        401: 'UNAUTHORIZED',
        403: 'FORBIDDEN',
        404: 'NOT_FOUND',
        405: 'METHOD_NOT_ALLOWED',
        409: 'CONFLICT',
        413: 'PAYLOAD_TOO_LARGE',
        415: 'UNSUPPORTED_MEDIA_TYPE',
        422: 'VALIDATION_ERROR',
        429: 'RATE_LIMITED',
    }.get(status_code, 'INTERNAL_ERROR' if status_code >= 500 else 'REQUEST_FAILED')
