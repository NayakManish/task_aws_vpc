"""
VPC Handler — Enhanced with typed exception handling + structured logging.

Logging design (optimised for pin-pointing 502 root causes in CloudWatch):

    * Every log line is one JSON object on one line, so CloudWatch Logs
      Insights can parse it natively (fields @timestamp, level, msg, ...).
    * Every line carries `requestId` (the Lambda request id) and, when
      available, `apiRequestId` (API Gateway's $context.requestId) so you
      can pivot between API-GW access logs and Lambda logs with a single
      Insights filter.
    * Module-level imports + env validation are wrapped so any cold-start
      failure is logged with the exception type/message BEFORE the Lambda
      bubbles up as Runtime.ImportModuleError (which shows up to the caller
      as a 502 with no useful body).
    * The handler logs: entry (method/path/apiRequestId), every exception
      branch with its type, and exit (statusCode + durationMs).

Routes: POST/GET/DELETE /vpc, GET /vpc/{id}
Author: Platform Engineering
"""
import json
import logging
import os
import sys
import time
import traceback
from typing import Any

# ---------------------------------------------------------------------------
# Logger safety net — runs BEFORE any logger.info/extra= call in this process.
# ---------------------------------------------------------------------------
# Python's logging.Logger.makeRecord raises
#     KeyError: "Attempt to overwrite '<key>' in LogRecord"
# if extra={} contains any key that shadows a built-in LogRecord attribute
# (name, msg, message, module, args, levelname, pathname, lineno, funcName,
# asctime, created, msecs, processName, threadName, exc_info, ...).
#
# A single log call with extra={"name": vpc_name} will crash the entire
# request and surface as a 500. This is a common-enough footgun that we
# simply auto-rename any conflicting key (prefixed with "x_") so a stray
# "name"/"msg"/"module" in a caller can never take out the Lambda.
_RESERVED_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime",
}
_orig_make_record = logging.Logger.makeRecord

def _safe_make_record(self, name, level, fn, lno, msg, args, exc_info,
                      func=None, extra=None, sinfo=None):
    if extra:
        extra = {
            (f"x_{k}" if k in _RESERVED_LOGRECORD_KEYS else k): v
            for k, v in extra.items()
        }
    return _orig_make_record(self, name, level, fn, lno, msg, args, exc_info,
                             func, extra, sinfo)

logging.Logger.makeRecord = _safe_make_record

# ---------------------------------------------------------------------------
# Structured JSON logger
# ---------------------------------------------------------------------------
# AWS Lambda attaches its own handler to the root logger. We add a JSON
# formatter so every record is machine-readable in CloudWatch.

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge any structured context passed via logger.xxx(..., extra={...})
        for key, value in record.__dict__.items():
            if key in payload or key.startswith("_"):
                continue
            if key in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message", "module",
                "msecs", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
            ):
                continue
            try:
                json.dumps(value)  # ensure serializable
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "stack": traceback.format_exception(*record.exc_info),
            }
        return json.dumps(payload, default=str)


def _configure_logger() -> logging.Logger:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(log_level)
    # Lambda's default handler writes plain text — swap its formatter so we
    # get JSON without duplicating handlers on warm starts.
    for h in root.handlers:
        h.setFormatter(_JsonFormatter())
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(_JsonFormatter())
        root.addHandler(h)
    return logging.getLogger("vpc_api")


logger = _configure_logger()
logger.info("cold_start", extra={
    "runtime": f"python{sys.version_info.major}.{sys.version_info.minor}",
    "region": os.environ.get("AWS_REGION"),
    "functionName": os.environ.get("AWS_LAMBDA_FUNCTION_NAME"),
    "functionVersion": os.environ.get("AWS_LAMBDA_FUNCTION_VERSION"),
    "logLevel": os.environ.get("LOG_LEVEL", "INFO"),
})

# ---------------------------------------------------------------------------
# Module-level imports — wrapped so any ImportError is visible in logs.
# Without this, Python raises before Lambda's logger is usable and the caller
# just sees "502 Bad Gateway" with no clue WHAT failed to import.
# ---------------------------------------------------------------------------
try:
    from botocore.exceptions import ClientError, ParamValidationError
    from models.vpc_model import VPCModel
    from models.response_model import APIResponse
    from utils.aws_helpers import create_vpc_resources, delete_vpc_resources
    from utils.validators import validate_create_vpc_request
    from utils.exceptions import (
        ValidationError, ResourceNotFoundError, ResourceConflictError,
        DependencyError, PartialFailureError, AWSThrottlingError,
        AWSPermissionError, InfrastructureError, VPCAPIError
    )
    logger.info("imports_ok")
except Exception as e:  # pragma: no cover — cold-start guard
    logger.critical("import_failed", extra={
        "errorType": type(e).__name__,
        "errorMessage": str(e),
        "sysPath": sys.path,
    }, exc_info=True)
    raise


# ---------------------------------------------------------------------------
# Environment validation — on failure we log AND raise so CloudWatch shows
# the reason, and Lambda refuses to initialise (fast-fail is better than
# silently 502-ing on every request).
# ---------------------------------------------------------------------------
def _validate_environment() -> None:
    required = ["DYNAMODB_TABLE_NAME"]
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        logger.critical("env_validation_failed", extra={"missing": missing})
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    logger.info("env_validation_ok", extra={
        "dynamodbTable": os.environ.get("DYNAMODB_TABLE_NAME"),
    })


_validate_environment()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def handler(event: dict, context: Any) -> dict:
    started_at    = time.time()
    request_id    = getattr(context, "aws_request_id", "unknown") if context else "unknown"
    request_ctx   = event.get("requestContext", {}) or {}
    api_request_id = request_ctx.get("requestId", "unknown")
    method        = event.get("httpMethod", "")
    path          = event.get("path", "")
    parameters    = event.get("pathParameters") or {}
    claims        = request_ctx.get("authorizer", {}).get("claims", {}) or {}
    user_email    = claims.get("email", "unknown")

    # Attach correlation IDs to every subsequent log line in this invocation.
    log_ctx = {"requestId": request_id, "apiRequestId": api_request_id,
               "method": method, "path": path, "user": user_email}

    logger.info("request_received", extra={
        **log_ctx,
        "sourceIp": request_ctx.get("identity", {}).get("sourceIp"),
        "userAgent": request_ctx.get("identity", {}).get("userAgent"),
        "hasBody": bool(event.get("body")),
    })

    body = _parse_body(event.get("body"), log_ctx)

    try:
        if method == "POST" and path == "/vpc":
            response = _create_vpc(body, user_email, log_ctx)
        elif method == "GET" and path == "/vpc":
            response = _list_vpcs(log_ctx)
        elif method == "GET" and "/vpc/" in path:
            response = _get_vpc(parameters.get("vpc_id"), log_ctx)
        elif method == "DELETE" and "/vpc/" in path:
            response = _delete_vpc(parameters.get("vpc_id"), user_email, log_ctx)
        else:
            logger.warning("route_not_found", extra=log_ctx)
            response = APIResponse.not_found(f"Route {method} {path} not found")

    except ValidationError as e:
        logger.warning("validation_error", extra={**log_ctx, "error": str(e)})
        response = APIResponse.bad_request(str(e))
    except ResourceNotFoundError as e:
        logger.warning("resource_not_found", extra={**log_ctx, "error": str(e)})
        response = APIResponse.not_found(str(e))
    except ResourceConflictError as e:
        logger.warning("resource_conflict", extra={**log_ctx, "error": str(e)})
        response = APIResponse.conflict(str(e))
    except DependencyError as e:
        logger.warning("dependency_error", extra={**log_ctx, "error": str(e)})
        response = APIResponse.conflict(str(e), data={"dependencies": e.dependencies})
    except PartialFailureError as e:
        logger.warning("partial_failure", extra={**log_ctx, "error": str(e)})
        response = APIResponse.partial_content(str(e), data={"created": e.created, "failed": e.failed, "vpc_id": e.vpc_id})
    except AWSThrottlingError as e:
        logger.warning("aws_throttled", extra={**log_ctx, "error": str(e)})
        response = APIResponse.too_many_requests(str(e))
    except AWSPermissionError as e:
        logger.warning("aws_permission_denied", extra={**log_ctx, "error": str(e)})
        response = APIResponse.forbidden(str(e))
    except ParamValidationError as e:
        # boto3 received bad kwargs — almost always means the request body is
        # missing or malformed. 400, not 500.
        logger.warning("param_validation_error", extra={
            **log_ctx, "error": str(e),
        })
        response = APIResponse.bad_request(f"Invalid parameter: {e}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        message = e.response.get("Error", {}).get("Message", str(e))
        logger.error("aws_client_error", extra={
            **log_ctx, "awsErrorCode": code, "awsErrorMessage": message,
        }, exc_info=True)
        if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
            response = APIResponse.forbidden(f"{code}: {message}")
        elif code in ("Throttling", "RequestLimitExceeded", "ThrottlingException"):
            response = APIResponse.too_many_requests(f"{code}: {message}")
        else:
            response = APIResponse.internal_error(f"AWS {code}: {message}")
    except (InfrastructureError, VPCAPIError) as e:
        logger.error("infra_or_api_error", extra={**log_ctx, "error": str(e)}, exc_info=True)
        response = APIResponse.internal_error(str(e))
    except Exception as e:
        # This branch is the usual suspect behind 500s: an exception we didn't
        # anticipate bubbles up here, we log the full traceback, and still
        # return a well-formed response so API GW gets valid JSON.
        logger.exception("unhandled_exception", extra={
            **log_ctx, "errorType": type(e).__name__, "errorMessage": str(e),
        })
        response = APIResponse.internal_error(f"{type(e).__name__}: {e}")

    duration_ms = int((time.time() - started_at) * 1000)
    # Validate response shape — if we ever return something that isn't a
    # proxy-format dict, API Gateway will 502. Detect it HERE instead.
    _validate_response_shape(response, log_ctx)
    logger.info("response_sent", extra={
        **log_ctx,
        "statusCode": response.get("statusCode"),
        "durationMs": duration_ms,
    })
    return response


# ---------------------------------------------------------------------------
# Route implementations
# ---------------------------------------------------------------------------
def _create_vpc(body, user_email, log_ctx):
    err = validate_create_vpc_request(body)
    if err:
        raise ValidationError(err)
    logger.info("create_vpc_start", extra={
        **log_ctx, "vpcName": body.get("name"), "cidr": body.get("cidr_block"),
        "subnetCount": len(body.get("subnets", [])),
    })
    result = create_vpc_resources(
        name=body["name"], cidr_block=body["cidr_block"],
        region=body.get("region", os.environ.get("AWS_REGION", "us-east-1")),
        subnets=body.get("subnets", []), tags=body.get("tags", {}), created_by=user_email,
    )
    logger.info("create_vpc_done", extra={
        **log_ctx, "vpcId": result.get("vpc_id"),
        "partial": bool(result.get("warnings", {}).get("partial_failure")),
    })
    if result.get("warnings", {}).get("partial_failure"):
        return APIResponse.partial_content("VPC created with partial subnet failures", data=result)
    return APIResponse.created(result)


def _list_vpcs(log_ctx):
    logger.info("list_vpcs_start", extra=log_ctx)
    vpcs = VPCModel().list_all()
    logger.info("list_vpcs_done", extra={**log_ctx, "count": len(vpcs)})
    return APIResponse.ok({"vpcs": vpcs, "count": len(vpcs)})


def _get_vpc(vpc_id, log_ctx):
    if not vpc_id:
        raise ValidationError("vpc_id required")
    logger.info("get_vpc_start", extra={**log_ctx, "vpcId": vpc_id})
    vpc = VPCModel().get_by_id(vpc_id)
    if not vpc:
        raise ResourceNotFoundError("VPC", vpc_id)
    return APIResponse.ok(vpc)


def _delete_vpc(vpc_id, user_email, log_ctx):
    if not vpc_id:
        raise ValidationError("vpc_id required")
    logger.info("delete_vpc_start", extra={**log_ctx, "vpcId": vpc_id})
    vpc = VPCModel().get_by_id(vpc_id)
    if not vpc:
        raise ResourceNotFoundError("VPC", vpc_id)
    delete_vpc_resources(
        vpc_id=vpc_id, region=vpc["region"],
        subnet_ids=[s["subnet_id"] for s in vpc.get("subnets", [])],
        deleted_by=user_email,
    )
    logger.info("delete_vpc_done", extra={**log_ctx, "vpcId": vpc_id})
    return APIResponse.ok({"message": f"VPC {vpc_id} deleted successfully"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_body(body, log_ctx):
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        # Don't fail the request here — let the validators produce a 400 with
        # the actual validation error. Just log so we can tell "bad JSON"
        # apart from "good JSON, bad fields".
        logger.warning("body_parse_failed", extra={**log_ctx, "error": str(e)})
        return {}


def _validate_response_shape(response, log_ctx) -> None:
    """
    API Gateway's Lambda proxy integration REQUIRES the response to be a dict
    with keys: statusCode (int), headers (dict), body (str). Anything else and
    API GW returns 502 with `Malformed Lambda proxy response`. Catch that
    failure mode here so the next line of the log tells us what went wrong
    instead of a mysterious gateway 502.
    """
    if not isinstance(response, dict):
        logger.error("malformed_response_not_dict", extra={
            **log_ctx, "responseType": type(response).__name__})
        return
    problems = []
    if not isinstance(response.get("statusCode"), int):
        problems.append("statusCode must be int")
    if not isinstance(response.get("headers"), dict):
        problems.append("headers must be dict")
    if not isinstance(response.get("body"), str):
        problems.append("body must be JSON string, not object")
    if problems:
        logger.error("malformed_response", extra={**log_ctx, "problems": problems})
