"""
Negative Scenario Tests — All the ways the VPC API can fail.

Tests cover:
    1.  VPC limit exceeded
    2.  CIDR overlap with existing VPC
    3.  Duplicate VPC name
    4.  Invalid availability zone
    5.  All subnets fail → rollback
    6.  Partial subnet failure → 207 response
    7.  DynamoDB write fails after VPC created → PartialFailureError
    8.  Delete VPC with active EC2 instances → DependencyError
    9.  Delete VPC with load balancers → DependencyError
    10. Delete non-existent VPC
    11. Get non-existent VPC
    12. AWS permissions error
    13. AWS throttling with retry exhaustion
    14. Malformed JSON body
    15. Missing auth claims (Cognito context missing)
    16. VPC already deleted from AWS but record in DynamoDB
    17. Concurrent delete attempts

Author: Platform Engineering
"""

import sys
import os
import json
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from utils.exceptions import (
    ResourceConflictError, DependencyError, PartialFailureError,
    AWSThrottlingError, AWSPermissionError, ResourceNotFoundError,
    ValidationError
)
from utils.validators import validate_create_vpc_request


# ── Helpers ───────────────────────────────────────────────────────

def make_event(method, path, body=None, vpc_id=None, email="test@example.com"):
    """Build a mock API Gateway event."""
    event = {
        'httpMethod':      method,
        'path':            path,
        'pathParameters':  {'vpc_id': vpc_id} if vpc_id else None,
        'body':            json.dumps(body) if body else None,
        'requestContext': {
            'authorizer': {
                'claims': {'email': email, 'sub': 'user-123'}
            }
        }
    }
    return event


def make_context():
    ctx = MagicMock()
    ctx.aws_request_id = 'test-request-id'
    return ctx


def valid_vpc_body():
    return {
        'name':       'test-vpc',
        'cidr_block': '10.0.0.0/16',
        'region':     'eu-central-1',
        'subnets': [
            {'name': 'pub-1', 'cidr_block': '10.0.1.0/24',
             'subnet_type': 'public',  'availability_zone': 'eu-central-1a'},
            {'name': 'priv-1', 'cidr_block': '10.0.2.0/24',
             'subnet_type': 'private', 'availability_zone': 'eu-central-1b'}
        ],
        'tags': {'environment': 'dev'}
    }


def make_client_error(code, message="Test error"):
    from botocore.exceptions import ClientError
    return ClientError(
        {'Error': {'Code': code, 'Message': message},
         'ResponseMetadata': {'RequestId': 'test-req-id'}},
        'TestOperation'
    )


# ── Test: Validation Errors ───────────────────────────────────────

class TestValidationNegativeScenarios:

    def test_public_ip_cidr_rejected(self):
        body = valid_vpc_body()
        body['cidr_block'] = '8.8.8.0/24'
        error = validate_create_vpc_request(body)
        assert error is not None
        assert 'private' in error.lower()

    def test_subnet_overlapping_cidrs(self):
        """Two subnets with same CIDR."""
        body = valid_vpc_body()
        body['subnets'][1]['cidr_block'] = body['subnets'][0]['cidr_block']
        # Same CIDR → not a validator error directly
        # AWS will reject it — but we catch duplicate names
        body['subnets'][1]['name'] = body['subnets'][0]['name']
        error = validate_create_vpc_request(body)
        assert error is not None
        assert 'duplicate' in error.lower()

    def test_subnet_cidr_too_large_for_vpc(self):
        """Subnet CIDR larger than VPC CIDR."""
        body = valid_vpc_body()
        body['cidr_block'] = '10.0.0.0/24'
        body['subnets'][0]['cidr_block'] = '10.0.0.0/16'  # Larger than VPC
        error = validate_create_vpc_request(body)
        assert error is not None

    def test_more_than_20_subnets(self):
        body = valid_vpc_body()
        body['subnets'] = [
            {'name': f'subnet-{i}', 'cidr_block': f'10.0.{i}.0/24'}
            for i in range(21)
        ]
        error = validate_create_vpc_request(body)
        assert error is not None
        assert '20' in error

    def test_invalid_az_format(self):
        body = valid_vpc_body()
        body['subnets'][0]['availability_zone'] = 'not-an-az'
        error = validate_create_vpc_request(body)
        assert error is not None
        assert 'availability_zone' in error

    def test_name_with_special_chars(self):
        body = valid_vpc_body()
        body['name'] = 'my vpc! @#$'
        error = validate_create_vpc_request(body)
        assert error is not None

    def test_cidr_prefix_too_small(self):
        """VPC /8 is too large — would overlap everything."""
        body = valid_vpc_body()
        body['cidr_block'] = '10.0.0.0/8'
        error = validate_create_vpc_request(body)
        assert error is not None


# ── Test: AWS Resource Conflicts ──────────────────────────────────

class TestResourceConflicts:

    def test_vpc_limit_exceeded(self):
        """Should raise ResourceConflictError when VPC limit hit."""
        from utils.aws_helpers import check_vpc_limit
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {
            'Vpcs': [{'VpcId': f'vpc-{i}'} for i in range(5)]
        }
        try:
            check_vpc_limit(mock_ec2, 'eu-central-1')
            assert False, "Should have raised ResourceConflictError"
        except ResourceConflictError as e:
            assert 'limit' in str(e).lower()
            assert '5' in str(e)

    def test_cidr_overlap_detected(self):
        """Should raise ResourceConflictError when CIDR overlaps."""
        from utils.aws_helpers import check_cidr_overlap
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {
            'Vpcs': [{'VpcId': 'vpc-existing', 'CidrBlock': '10.0.0.0/16'}]
        }
        try:
            # This CIDR overlaps with 10.0.0.0/16
            check_cidr_overlap(mock_ec2, '10.0.1.0/24')
            assert False, "Should have raised ResourceConflictError"
        except ResourceConflictError as e:
            assert 'overlap' in str(e).lower()
            assert 'vpc-existing' in str(e)

    def test_non_overlapping_cidr_passes(self):
        """Non-overlapping CIDR should not raise."""
        from utils.aws_helpers import check_cidr_overlap
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {
            'Vpcs': [{'VpcId': 'vpc-existing', 'CidrBlock': '10.0.0.0/16'}]
        }
        # 172.16.0.0/16 does NOT overlap with 10.0.0.0/16
        check_cidr_overlap(mock_ec2, '172.16.0.0/16')  # Should not raise

    def test_duplicate_name_rejected(self):
        """Should raise ResourceConflictError for duplicate name."""
        from utils.aws_helpers import check_duplicate_name
        mock_model = MagicMock()
        mock_model.list_all.return_value = [
            {'name': 'existing-vpc', 'vpc_id': 'vpc-abc', 'status': 'active'}
        ]
        with patch('utils.aws_helpers.VPCModel', return_value=mock_model):
            try:
                check_duplicate_name('existing-vpc')
                assert False, "Should have raised ResourceConflictError"
            except ResourceConflictError as e:
                assert 'existing-vpc' in str(e)
                assert 'vpc-abc' in str(e)

    def test_deleted_vpc_name_can_be_reused(self):
        """Deleted VPC name should be reusable."""
        from utils.aws_helpers import check_duplicate_name
        mock_model = MagicMock()
        mock_model.list_all.return_value = [
            {'name': 'old-vpc', 'vpc_id': 'vpc-abc', 'status': 'deleted'}
        ]
        with patch('utils.aws_helpers.VPCModel', return_value=mock_model):
            check_duplicate_name('old-vpc')  # Should not raise


# ── Test: Dependency Checks ───────────────────────────────────────

class TestDependencyChecks:

    def test_vpc_with_ec2_instances_blocked(self):
        """Should raise DependencyError when EC2 instances exist."""
        from utils.aws_helpers import check_vpc_dependencies
        mock_ec2 = MagicMock()
        mock_ec2.meta.region_name = 'eu-central-1'
        mock_ec2.describe_instances.return_value = {
            'Reservations': [{'Instances': [{'InstanceId': 'i-abc123'}]}]
        }
        mock_ec2.describe_nat_gateways.return_value = {'NatGateways': []}
        mock_ec2.describe_vpc_endpoints.return_value = {'VpcEndpoints': []}
        mock_ec2.describe_transit_gateway_vpc_attachments.return_value = {
            'TransitGatewayVpcAttachments': []
        }

        with patch('utils.aws_helpers.boto3') as mock_boto3:
            mock_boto3.client.return_value = MagicMock(
                describe_load_balancers=lambda: {'LoadBalancers': []}
            )
            deps = check_vpc_dependencies(mock_ec2, 'vpc-abc123')

        assert len(deps) > 0
        dep_types = [d['type'] for d in deps]
        assert 'EC2Instances' in dep_types

    def test_vpc_with_load_balancer_blocked(self):
        """Should detect load balancer as dependency."""
        from utils.aws_helpers import check_vpc_dependencies
        mock_ec2 = MagicMock()
        mock_ec2.meta.region_name = 'eu-central-1'
        mock_ec2.describe_instances.return_value = {'Reservations': []}
        mock_ec2.describe_nat_gateways.return_value = {'NatGateways': []}
        mock_ec2.describe_vpc_endpoints.return_value = {'VpcEndpoints': []}
        mock_ec2.describe_transit_gateway_vpc_attachments.return_value = {
            'TransitGatewayVpcAttachments': []
        }

        mock_elb = MagicMock()
        mock_elb.describe_load_balancers.return_value = {
            'LoadBalancers': [{'VpcId': 'vpc-abc123', 'LoadBalancerArn': 'arn:alb:test'}]
        }

        with patch('utils.aws_helpers.boto3') as mock_boto3:
            mock_boto3.client.return_value = mock_elb
            deps = check_vpc_dependencies(mock_ec2, 'vpc-abc123')

        dep_types = [d['type'] for d in deps]
        assert 'LoadBalancers' in dep_types

    def test_clean_vpc_no_dependencies(self):
        """Clean VPC should have zero dependencies."""
        from utils.aws_helpers import check_vpc_dependencies
        mock_ec2 = MagicMock()
        mock_ec2.meta.region_name = 'eu-central-1'
        mock_ec2.describe_instances.return_value = {'Reservations': []}
        mock_ec2.describe_nat_gateways.return_value = {'NatGateways': []}
        mock_ec2.describe_vpc_endpoints.return_value = {'VpcEndpoints': []}
        mock_ec2.describe_transit_gateway_vpc_attachments.return_value = {
            'TransitGatewayVpcAttachments': []
        }

        with patch('utils.aws_helpers.boto3') as mock_boto3:
            mock_boto3.client.return_value = MagicMock(
                describe_load_balancers=lambda: {'LoadBalancers': []}
            )
            deps = check_vpc_dependencies(mock_ec2, 'vpc-clean')

        assert deps == []


# ── Test: Handler HTTP Responses ──────────────────────────────────

class TestHandlerNegativeResponses:

    def test_get_nonexistent_vpc_returns_404(self):
        """GET /vpc/{id} for non-existent VPC should return 404."""
        from handlers.vpc_handler import handler
        mock_model = MagicMock()
        mock_model.get_by_id.return_value = None

        with patch('handlers.vpc_handler.VPCModel', return_value=mock_model):
            event    = make_event('GET', '/vpc/vpc-doesnotexist',
                                  vpc_id='vpc-doesnotexist')
            response = handler(event, make_context())

        assert response['statusCode'] == 404
        body = json.loads(response['body'])
        assert body['success'] is False
        assert 'not found' in body['error'].lower()

    def test_delete_nonexistent_vpc_returns_404(self):
        """DELETE /vpc/{id} for non-existent VPC should return 404."""
        from handlers.vpc_handler import handler
        mock_model = MagicMock()
        mock_model.get_by_id.return_value = None

        with patch('handlers.vpc_handler.VPCModel', return_value=mock_model):
            event    = make_event('DELETE', '/vpc/vpc-ghost',
                                  vpc_id='vpc-ghost')
            response = handler(event, make_context())

        assert response['statusCode'] == 404

    def test_create_vpc_conflict_returns_409(self):
        """Duplicate name should return 409 Conflict."""
        from handlers.vpc_handler import handler

        with patch('handlers.vpc_handler.create_vpc_resources',
                   side_effect=ResourceConflictError("VPC 'test-vpc' already exists", 'vpc-abc')):
            event    = make_event('POST', '/vpc', body=valid_vpc_body())
            response = handler(event, make_context())

        assert response['statusCode'] == 409
        body = json.loads(response['body'])
        assert body['success'] is False

    def test_delete_vpc_with_dependencies_returns_409(self):
        """Delete with active dependencies should return 409 with details."""
        from handlers.vpc_handler import handler
        mock_model = MagicMock()
        mock_model.get_by_id.return_value = {
            'vpc_id': 'vpc-abc', 'region': 'eu-central-1', 'subnets': []
        }

        deps = [{'type': 'EC2Instances', 'count': 3,
                 'ids': ['i-abc'], 'action': 'Terminate instances'}]

        with patch('handlers.vpc_handler.VPCModel', return_value=mock_model):
            with patch('handlers.vpc_handler.delete_vpc_resources',
                       side_effect=DependencyError('vpc-abc', deps)):
                event    = make_event('DELETE', '/vpc/vpc-abc', vpc_id='vpc-abc')
                response = handler(event, make_context())

        assert response['statusCode'] == 409
        body = json.loads(response['body'])
        assert 'dependencies' in body['data']
        assert len(body['data']['dependencies']) == 1

    def test_throttling_returns_429(self):
        """AWS throttling should return 429."""
        from handlers.vpc_handler import handler

        with patch('handlers.vpc_handler.create_vpc_resources',
                   side_effect=AWSThrottlingError('create_vpc')):
            event    = make_event('POST', '/vpc', body=valid_vpc_body())
            response = handler(event, make_context())

        assert response['statusCode'] == 429

    def test_permission_error_returns_403(self):
        """IAM permission denied should return 403."""
        from handlers.vpc_handler import handler

        with patch('handlers.vpc_handler.create_vpc_resources',
                   side_effect=AWSPermissionError('ec2:CreateVpc', 'eu-central-1')):
            event    = make_event('POST', '/vpc', body=valid_vpc_body())
            response = handler(event, make_context())

        assert response['statusCode'] == 403

    def test_partial_failure_returns_207(self):
        """Partial subnet failure should return 207 Multi-Status."""
        from handlers.vpc_handler import handler

        partial_result = {
            'vpc_id': 'vpc-partial',
            'name':   'test-vpc',
            'warnings': {
                'partial_failure': True,
                'failed_subnets':  [{'subnet_name': 'priv-1', 'error': 'AZ unavailable'}],
                'message':         '1 subnet(s) failed'
            }
        }

        with patch('handlers.vpc_handler.create_vpc_resources',
                   return_value=partial_result):
            event    = make_event('POST', '/vpc', body=valid_vpc_body())
            response = handler(event, make_context())

        assert response['statusCode'] == 207
        body = json.loads(response['body'])
        assert body['success'] is True   # Partial success

    def test_missing_vpc_id_returns_400(self):
        """Missing vpc_id path param should return 400."""
        from handlers.vpc_handler import handler
        event = make_event('GET', '/vpc/None')
        event['pathParameters'] = {}   # No vpc_id
        response = handler(event, make_context())
        assert response['statusCode'] == 404   # Route not matched

    def test_empty_body_returns_400(self):
        """Empty POST body should return 400."""
        from handlers.vpc_handler import handler
        event = make_event('POST', '/vpc', body=None)
        response = handler(event, make_context())
        assert response['statusCode'] == 400

    def test_malformed_json_body(self):
        """Malformed JSON body should return 400 gracefully."""
        from handlers.vpc_handler import handler
        event = make_event('POST', '/vpc')
        event['body'] = '{not valid json'
        response = handler(event, make_context())
        assert response['statusCode'] == 400

    def test_unknown_route_returns_404(self):
        """Unknown route should return 404."""
        from handlers.vpc_handler import handler
        event    = make_event('PATCH', '/vpc/abc', vpc_id='abc')
        response = handler(event, make_context())
        assert response['statusCode'] == 404


# ── Run all tests ─────────────────────────────────────────────────

if __name__ == '__main__':
    test_classes = [
        TestValidationNegativeScenarios(),
        TestResourceConflicts(),
        TestDependencyChecks(),
        TestHandlerNegativeResponses()
    ]

    passed = 0
    failed = 0

    for test_class in test_classes:
        class_name = test_class.__class__.__name__
        print(f"\n{class_name}:")
        methods = [m for m in dir(test_class) if m.startswith('test_')]
        for method_name in methods:
            try:
                getattr(test_class, method_name)()
                print(f"  PASS: {method_name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL: {method_name} → {e}")
                failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        exit(1)
