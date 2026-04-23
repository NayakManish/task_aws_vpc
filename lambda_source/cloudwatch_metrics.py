"""
CloudWatch Metrics — Custom metric emission for application-level events.

Emits metrics that AWS doesn't provide natively:
    - PartialFailure:    VPC created but some subnets failed
    - RollbackTriggered: VPC creation completely rolled back
    - DependencyBlock:   VPC deletion blocked by active resources
    - VPCCount:          Current number of tracked VPCs (quota monitoring)

These metrics power the custom CloudWatch alarms in alarms.tf.

Usage:
    from utils.cloudwatch_metrics import emit_metric, MetricName
    emit_metric(MetricName.PARTIAL_FAILURE, vpc_id="vpc-abc123")

Author: Platform Engineering
"""

import logging
import os
from enum import Enum

import boto3
from botocore.exceptions import ClientError

logger     = logging.getLogger(__name__)
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'dev')
REGION      = os.environ.get('AWS_REGION', 'eu-central-1')
NAMESPACE   = 'VPCApi/Operations'


class MetricName(str, Enum):
    PARTIAL_FAILURE    = 'PartialFailure'
    ROLLBACK_TRIGGERED = 'RollbackTriggered'
    DEPENDENCY_BLOCK   = 'DependencyBlock'
    VPC_COUNT          = 'VPCCount'
    VPC_CREATED        = 'VPCCreated'
    VPC_DELETED        = 'VPCDeleted'
    CREATE_DURATION    = 'VPCCreateDurationMs'


def emit_metric(
    metric_name: MetricName,
    value:       float = 1.0,
    unit:        str   = 'Count',
    extra_dims:  dict  = None
) -> None:
    """
    Emit a custom metric to CloudWatch.

    Args:
        metric_name: MetricName enum value
        value:       Metric value (default 1 for count-based metrics)
        unit:        CloudWatch unit string
        extra_dims:  Additional dimensions beyond Environment/Region

    Silently logs on failure — metric emission must never break the API.
    """
    cw = boto3.client('cloudwatch', region_name=REGION)

    dimensions = [
        {'Name': 'Environment', 'Value': ENVIRONMENT}
    ]

    if metric_name == MetricName.VPC_COUNT:
        dimensions.append({'Name': 'Region', 'Value': REGION})

    if extra_dims:
        dimensions.extend([
            {'Name': k, 'Value': str(v)}
            for k, v in extra_dims.items()
        ])

    try:
        cw.put_metric_data(
            Namespace  = NAMESPACE,
            MetricData = [{
                'MetricName': metric_name.value,
                'Value':      value,
                'Unit':       unit,
                'Dimensions': dimensions
            }]
        )
        logger.debug("Metric emitted", extra={
            "metric":  metric_name.value,
            "value":   value,
            "dims":    dimensions
        })

    except ClientError as e:
        # Never let metric emission break the main operation
        logger.warning("CloudWatch metric emission failed", extra={
            "metric": metric_name.value,
            "error":  str(e)
        })


def emit_vpc_count(current_count: int) -> None:
    """Emit current VPC count for quota monitoring."""
    emit_metric(MetricName.VPC_COUNT, value=float(current_count))


def emit_create_duration(duration_ms: float) -> None:
    """Emit VPC creation duration for performance tracking."""
    emit_metric(
        MetricName.CREATE_DURATION,
        value=duration_ms,
        unit='Milliseconds'
    )
