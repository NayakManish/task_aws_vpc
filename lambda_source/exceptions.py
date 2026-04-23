"""
Custom Exceptions — Typed error hierarchy for VPC API.

Every failure mode has a specific exception type.
This allows callers to handle errors precisely
without parsing error strings.

Hierarchy:
    VPCAPIError (base)
    ├── ValidationError          → 400 Bad Request
    ├── ResourceNotFoundError    → 404 Not Found
    ├── ResourceConflictError    → 409 Conflict
    ├── DependencyError          → 409 Conflict (has dependents)
    ├── PartialFailureError      → 207 Multi-Status
    ├── AWSThrottlingError       → 429 Too Many Requests
    ├── AWSPermissionError       → 403 Forbidden
    └── InfrastructureError      → 500 Internal Server Error

Author: Platform Engineering
"""


class VPCAPIError(Exception):
    """
    Base exception for all VPC API errors.
    Always carries a human-readable message and
    optional metadata for structured logging.
    """
    def __init__(self, message: str, metadata: dict = None):
        super().__init__(message)
        self.message  = message
        self.metadata = metadata or {}


class ValidationError(VPCAPIError):
    """
    Request payload failed validation.
    Maps to HTTP 400.

    Example: Invalid CIDR, missing required field,
             subnet outside VPC range.
    """
    pass


class ResourceNotFoundError(VPCAPIError):
    """
    Requested resource does not exist.
    Maps to HTTP 404.

    Example: VPC ID not in DynamoDB,
             VPC deleted from AWS but record exists.
    """
    def __init__(self, resource_type: str, resource_id: str):
        super().__init__(
            f"{resource_type} '{resource_id}' not found",
            metadata={
                "resource_type": resource_type,
                "resource_id":   resource_id
            }
        )


class ResourceConflictError(VPCAPIError):
    """
    Operation conflicts with current resource state.
    Maps to HTTP 409.

    Example: Creating VPC with name that already exists,
             deleting a VPC that is already being deleted.
    """
    def __init__(self, message: str, resource_id: str = None):
        super().__init__(
            message,
            metadata={"resource_id": resource_id}
        )


class DependencyError(VPCAPIError):
    """
    Cannot complete operation because resource has active dependencies.
    Maps to HTTP 409.

    Example: Attempting to delete a VPC that has:
             - Running EC2 instances
             - Active RDS instances
             - Lambda functions in the VPC
             - EKS node groups
             - Load balancers
    """
    def __init__(self, vpc_id: str, dependencies: list):
        super().__init__(
            f"VPC '{vpc_id}' has active dependencies and cannot be deleted",
            metadata={
                "vpc_id":       vpc_id,
                "dependencies": dependencies
            }
        )
        self.dependencies = dependencies


class PartialFailureError(VPCAPIError):
    """
    Operation partially succeeded — some resources created,
    some failed. Requires manual cleanup or rollback.
    Maps to HTTP 207 Multi-Status.

    Example: VPC created successfully but 2 of 4 subnets failed.
             VPC exists in AWS but record not in DynamoDB.
    """
    def __init__(
        self,
        message:  str,
        created:  list = None,
        failed:   list = None,
        vpc_id:   str  = None
    ):
        super().__init__(
            message,
            metadata={
                "vpc_id":          vpc_id,
                "created_resources": created or [],
                "failed_resources":  failed or []
            }
        )
        self.created = created or []
        self.failed  = failed  or []
        self.vpc_id  = vpc_id


class AWSThrottlingError(VPCAPIError):
    """
    AWS API rate limit exceeded — caller should retry with backoff.
    Maps to HTTP 429.
    """
    def __init__(self, operation: str):
        super().__init__(
            f"AWS API throttled on operation: {operation}. Retry after backoff.",
            metadata={"operation": operation}
        )


class AWSPermissionError(VPCAPIError):
    """
    Lambda IAM role lacks permission for the requested operation.
    Maps to HTTP 403. NOT retryable — requires IAM fix.
    """
    def __init__(self, operation: str, resource: str = None):
        super().__init__(
            f"Permission denied: {operation}",
            metadata={
                "operation": operation,
                "resource":  resource
            }
        )


class InfrastructureError(VPCAPIError):
    """
    Unexpected AWS infrastructure error.
    Maps to HTTP 500. May or may not be retryable.
    """
    def __init__(self, message: str, aws_error_code: str = None):
        super().__init__(
            message,
            metadata={"aws_error_code": aws_error_code}
        )
