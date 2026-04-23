"""
Response Model — Standardized API Gateway response builder.

Ensures consistent response format across all endpoints:
{
    "success": true/false,
    "data": {...} or null,
    "error": null or "error message",
    "timestamp": "ISO 8601"
}

Author: Platform Engineering
"""

import json
from datetime import datetime, timezone
from decimal import Decimal


class DecimalEncoder(json.JSONEncoder):
    """
    Custom JSON encoder to handle DynamoDB Decimal types.
    DynamoDB returns numbers as Decimal — JSON doesn't support it.
    """
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


class APIResponse:
    """
    Static factory class for building API Gateway responses.

    All methods return a dict compatible with API Gateway
    Lambda proxy integration format.
    """

    # CORS headers — required for browser-based API clients
    _HEADERS = {
        'Content-Type':                'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS'
    }

    @staticmethod
    def _build(status_code: int, success: bool,
               data: dict = None, error: str = None) -> dict:
        """Build standardized API Gateway response."""
        body = {
            'success':   success,
            'data':      data,
            'error':     error,
            'timestamp': datetime.now(timezone.utc).isoformat()
        }
        return {
            'statusCode': status_code,
            'headers':    APIResponse._HEADERS,
            'body':       json.dumps(body, cls=DecimalEncoder)
        }

    @staticmethod
    def ok(data: dict) -> dict:
        """200 OK — successful retrieval."""
        return APIResponse._build(200, True, data=data)

    @staticmethod
    def created(data: dict) -> dict:
        """201 Created — resource successfully created."""
        return APIResponse._build(201, True, data=data)

    @staticmethod
    def bad_request(error: str) -> dict:
        """400 Bad Request — invalid input."""
        return APIResponse._build(400, False, error=error)

    @staticmethod
    def unauthorized(error: str = "Unauthorized") -> dict:
        """401 Unauthorized — missing or invalid auth."""
        return APIResponse._build(401, False, error=error)

    @staticmethod
    def not_found(error: str) -> dict:
        """404 Not Found — resource doesn't exist."""
        return APIResponse._build(404, False, error=error)

    @staticmethod
    def internal_error(error: str = "Internal server error") -> dict:
        """500 Internal Server Error — unexpected failure."""
        return APIResponse._build(500, False, error=error)

    @staticmethod
    def conflict(error: str, data: dict = None) -> dict:
        """409 Conflict — resource conflict or dependency block."""
        return APIResponse._build(409, False, data=data, error=error)

    @staticmethod
    def partial_content(message: str, data: dict = None) -> dict:
        """207 Multi-Status — partial success."""
        return APIResponse._build(207, True, data=data, error=message)

    @staticmethod
    def too_many_requests(error: str) -> dict:
        """429 Too Many Requests — AWS throttled."""
        return APIResponse._build(429, False, error=error)

    @staticmethod
    def forbidden(error: str) -> dict:
        """403 Forbidden — permission denied."""
        return APIResponse._build(403, False, error=error)
