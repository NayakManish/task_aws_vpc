"""
VPC Handler — Enhanced with typed exception handling.
Routes: POST/GET/DELETE /vpc, GET /vpc/{id}
Author: Platform Engineering
"""
import json, logging, os
from typing import Any
from models.vpc_model import VPCModel
from models.response_model import APIResponse
from utils.aws_helpers import create_vpc_resources, delete_vpc_resources
from utils.validators import validate_create_vpc_request
from utils.exceptions import (
    ValidationError, ResourceNotFoundError, ResourceConflictError,
    DependencyError, PartialFailureError, AWSThrottlingError,
    AWSPermissionError, InfrastructureError, VPCAPIError
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def handler(event: dict, context: Any) -> dict:
    method     = event.get('httpMethod', '')
    path       = event.get('path', '')
    parameters = event.get('pathParameters') or {}
    body       = _parse_body(event.get('body'))
    claims     = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    user_email = claims.get('email', 'unknown')

    logger.info("Request", extra={"method": method, "path": path, "user": user_email})

    try:
        if method == 'POST' and path == '/vpc':
            return _create_vpc(body, user_email)
        elif method == 'GET' and path == '/vpc':
            return _list_vpcs()
        elif method == 'GET' and '/vpc/' in path:
            return _get_vpc(parameters.get('vpc_id'))
        elif method == 'DELETE' and '/vpc/' in path:
            return _delete_vpc(parameters.get('vpc_id'), user_email)
        else:
            return APIResponse.not_found(f"Route {method} {path} not found")

    except ValidationError as e:
        return APIResponse.bad_request(str(e))
    except ResourceNotFoundError as e:
        return APIResponse.not_found(str(e))
    except (ResourceConflictError) as e:
        return APIResponse.conflict(str(e))
    except DependencyError as e:
        return APIResponse.conflict(str(e), data={"dependencies": e.dependencies})
    except PartialFailureError as e:
        return APIResponse.partial_content(str(e), data={"created": e.created, "failed": e.failed, "vpc_id": e.vpc_id})
    except AWSThrottlingError as e:
        return APIResponse.too_many_requests(str(e))
    except AWSPermissionError as e:
        return APIResponse.forbidden(str(e))
    except (InfrastructureError, VPCAPIError) as e:
        return APIResponse.internal_error(str(e))
    except Exception as e:
        logger.exception("Unhandled exception")
        return APIResponse.internal_error("An unexpected error occurred")

def _create_vpc(body, user_email):
    err = validate_create_vpc_request(body)
    if err:
        raise ValidationError(err)
    result = create_vpc_resources(
        name=body['name'], cidr_block=body['cidr_block'],
        region=body.get('region', os.environ.get('AWS_REGION', 'eu-central-1')),
        subnets=body.get('subnets', []), tags=body.get('tags', {}), created_by=user_email
    )
    if result.get('warnings', {}).get('partial_failure'):
        return APIResponse.partial_content("VPC created with partial subnet failures", data=result)
    return APIResponse.created(result)

def _list_vpcs():
    vpcs = VPCModel().list_all()
    return APIResponse.ok({"vpcs": vpcs, "count": len(vpcs)})

def _get_vpc(vpc_id):
    if not vpc_id:
        raise ValidationError("vpc_id required")
    vpc = VPCModel().get_by_id(vpc_id)
    if not vpc:
        raise ResourceNotFoundError("VPC", vpc_id)
    return APIResponse.ok(vpc)

def _delete_vpc(vpc_id, user_email):
    if not vpc_id:
        raise ValidationError("vpc_id required")
    vpc = VPCModel().get_by_id(vpc_id)
    if not vpc:
        raise ResourceNotFoundError("VPC", vpc_id)
    delete_vpc_resources(vpc_id=vpc_id, region=vpc['region'],
                         subnet_ids=[s['subnet_id'] for s in vpc.get('subnets', [])],
                         deleted_by=user_email)
    return APIResponse.ok({"message": f"VPC {vpc_id} deleted successfully"})

def _parse_body(body):
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}
