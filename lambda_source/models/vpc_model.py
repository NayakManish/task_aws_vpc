"""
VPC Model — DynamoDB data access layer for VPC resources.

Table Schema:
    PK (partition key): vpc_id       → AWS VPC ID (vpc-xxxxxxxx)
    SK (sort key):      created_at   → ISO 8601 timestamp

    Additional attributes:
        name, cidr_block, region, subnets,
        tags, status, created_by, deleted_at, deleted_by

All write operations use condition expressions to prevent
accidental overwrites. Reads use strongly consistent reads
for data accuracy.

Author: Platform Engineering
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class VPCModel:
    """
    Data access object for VPC resources stored in DynamoDB.

    Usage:
        model = VPCModel()
        model.save(vpc_data)
        model.get_by_id('vpc-abc123')
        model.list_all()
        model.mark_deleted('vpc-abc123', 'user@example.com')
    """

    def __init__(self):
        self._dynamodb  = boto3.resource('dynamodb')
        self._table     = self._dynamodb.Table(
            os.environ['DYNAMODB_TABLE_NAME']
        )

    def save(self, vpc_data: dict) -> dict:
        """
        Persist a newly created VPC record to DynamoDB.

        Args:
            vpc_data: Dict containing vpc_id, name, cidr_block,
                      region, subnets, tags, created_by

        Returns:
            The saved record with timestamps added

        Raises:
            ValueError: If vpc_id already exists (idempotency check)
            ClientError: On DynamoDB write failure
        """
        now = datetime.now(timezone.utc).isoformat()

        item = {
            **vpc_data,
            'created_at': now,
            'updated_at': now,
            'status':     'active'
        }

        try:
            self._table.put_item(
                Item=item,
                # Prevent overwriting existing records
                ConditionExpression=Attr('vpc_id').not_exists()
            )
            logger.info("VPC record saved", extra={
                "vpc_id": vpc_data.get('vpc_id')
            })
            return item

        except ClientError as e:
            if e.response['Error']['Code'] == \
               'ConditionalCheckFailedException':
                raise ValueError(
                    f"VPC {vpc_data.get('vpc_id')} already exists"
                )
            logger.error("DynamoDB save failed", extra={
                "vpc_id":     vpc_data.get('vpc_id'),
                "error_code": e.response['Error']['Code'],
                "error":      str(e)
            })
            raise

    def get_by_id(self, vpc_id: str) -> Optional[dict]:
        """
        Retrieve a VPC record by its AWS VPC ID.

        Args:
            vpc_id: AWS VPC ID (vpc-xxxxxxxx)

        Returns:
            VPC record dict or None if not found
        """
        try:
            response = self._table.get_item(
                Key={'vpc_id': vpc_id},
                ConsistentRead=True  # Strong consistency for accuracy
            )
            return response.get('Item')

        except ClientError as e:
            logger.error("DynamoDB get failed", extra={
                "vpc_id":     vpc_id,
                "error_code": e.response['Error']['Code']
            })
            raise

    def list_all(self) -> list:
        """
        Retrieve all VPC records from DynamoDB.

        Note: Uses scan operation — acceptable for this use case
        as the table will contain a limited number of VPC records.
        For large datasets, implement pagination with LastEvaluatedKey.

        Returns:
            List of VPC record dicts, sorted by created_at descending
        """
        try:
            # Filter out deleted records in expression
            response = self._table.scan(
                FilterExpression=Attr('status').eq('active')
            )
            items = response.get('Items', [])

            # Handle pagination if dataset grows
            while 'LastEvaluatedKey' in response:
                response = self._table.scan(
                    FilterExpression=Attr('status').eq('active'),
                    ExclusiveStartKey=response['LastEvaluatedKey']
                )
                items.extend(response.get('Items', []))

            # Sort by creation time — newest first
            return sorted(
                items,
                key=lambda x: x.get('created_at', ''),
                reverse=True
            )

        except ClientError as e:
            logger.error("DynamoDB scan failed", extra={
                "error_code": e.response['Error']['Code']
            })
            raise

    def mark_deleted(self, vpc_id: str, deleted_by: str) -> None:
        """
        Soft delete — mark VPC record as deleted without removing it.
        Preserves audit trail of all created resources.

        Args:
            vpc_id:     AWS VPC ID to mark as deleted
            deleted_by: Email of user who triggered deletion
        """
        now = datetime.now(timezone.utc).isoformat()

        try:
            self._table.update_item(
                Key={'vpc_id': vpc_id},
                UpdateExpression=
                    'SET #status = :deleted, '
                    'deleted_at = :now, '
                    'deleted_by = :deleted_by, '
                    'updated_at = :now',
                ExpressionAttributeNames={
                    '#status': 'status'  # 'status' is reserved word
                },
                ExpressionAttributeValues={
                    ':deleted':    'deleted',
                    ':now':        now,
                    ':deleted_by': deleted_by
                },
                # Only update if record exists and is active
                ConditionExpression=
                    Attr('vpc_id').exists() &
                    Attr('status').eq('active')
            )
            logger.info("VPC marked as deleted", extra={
                "vpc_id":     vpc_id,
                "deleted_by": deleted_by
            })

        except ClientError as e:
            if e.response['Error']['Code'] == \
               'ConditionalCheckFailedException':
                raise ValueError(
                    f"VPC {vpc_id} not found or already deleted"
                )
            raise
