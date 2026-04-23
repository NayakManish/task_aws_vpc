"""
AWS Helpers

Scenarios covered:
    1. Dependency check before VPC deletion
    2. Partial failure handling with automatic rollback
    3. VPC limit check before attempting creation
    4. CIDR conflict detection
    5. Subnet AZ availability check
    6. Duplicate name check (idempotency guard)
    7. AWS throttling with exponential backoff

"""

import logging
import time
import os
from typing import Optional
import ipaddress

import boto3
from botocore.exceptions import ClientError, ParamValidationError

from models.vpc_model import VPCModel
from utils.exceptions import (
    DependencyError, PartialFailureError, AWSThrottlingError,
    AWSPermissionError, InfrastructureError, ResourceConflictError,
    ValidationError
)

logger = logging.getLogger(__name__)
VPC_LIMIT = int(os.environ.get('VPC_LIMIT', '5'))


def _step(name: str, **fields):
    """Uniform per-step log — makes it trivial to grep
    `msg = "create_step"` in CloudWatch to see exactly where a create
    got to before a 500."""
    logger.info("create_step", extra={"step": name, **fields})


def _with_backoff(func, *args, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in ('RequestLimitExceeded', 'Throttling', 'ThrottlingException'):
                wait = (2 ** attempt)
                logger.warning("AWS throttled", extra={"attempt": attempt + 1, "wait": wait})
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
                raise AWSThrottlingError(func.__name__)
            raise


def check_vpc_limit(ec2, region: str) -> None:
    response = ec2.describe_vpcs(Filters=[{'Name': 'state', 'Values': ['available', 'pending']}])
    count = len(response['Vpcs'])
    if count >= VPC_LIMIT:
        raise ResourceConflictError(
            f"VPC limit reached in {region}: {count}/{VPC_LIMIT}. Request a limit increase.",
            resource_id=region
        )


def check_cidr_overlap(ec2, new_cidr: str) -> None:
    new_network = ipaddress.ip_network(new_cidr, strict=False)
    response = ec2.describe_vpcs(Filters=[{'Name': 'state', 'Values': ['available']}])
    for vpc in response['Vpcs']:
        existing = ipaddress.ip_network(vpc['CidrBlock'], strict=False)
        if new_network.overlaps(existing):
            raise ResourceConflictError(
                f"CIDR '{new_cidr}' overlaps with VPC '{vpc['VpcId']}' ({vpc['CidrBlock']}). Choose a non-overlapping CIDR.",
                resource_id=vpc['VpcId']
            )


def check_duplicate_name(name: str) -> None:
    model = VPCModel()
    vpcs  = model.list_all()
    for vpc in vpcs:
        if vpc.get('name') == name and vpc.get('status') == 'active':
            raise ResourceConflictError(
                f"VPC named '{name}' already exists: {vpc['vpc_id']}.",
                resource_id=vpc['vpc_id']
            )


def validate_availability_zones(ec2, subnets: list, region: str) -> None:
    response = ec2.describe_availability_zones(Filters=[{'Name': 'state', 'Values': ['available']}])
    available_azs = {az['ZoneName'] for az in response['AvailabilityZones']}
    for subnet in subnets:
        az = subnet.get('availability_zone')
        if az and az not in available_azs:
            raise ValidationError(
                f"AZ '{az}' not available in {region}. Available: {sorted(available_azs)}"
            )


def check_vpc_dependencies(ec2, vpc_id: str) -> list:
    dependencies = []

    try:
        r = ec2.describe_instances(Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'instance-state-name', 'Values': ['running', 'stopped', 'stopping', 'pending']}
        ])
        instances = [i for res in r['Reservations'] for i in res['Instances']]
        if instances:
            dependencies.append({
                'type': 'EC2Instances', 'count': len(instances),
                'ids': [i['InstanceId'] for i in instances[:5]],
                'action': 'Terminate all EC2 instances before deleting VPC'
            })
    except ClientError:
        pass

    try:
        elb = boto3.client('elbv2', region_name=ec2.meta.region_name)
        lbs = [lb for lb in elb.describe_load_balancers()['LoadBalancers'] if lb.get('VpcId') == vpc_id]
        if lbs:
            dependencies.append({
                'type': 'LoadBalancers', 'count': len(lbs),
                'ids': [lb['LoadBalancerArn'] for lb in lbs[:5]],
                'action': 'Delete all load balancers before deleting VPC'
            })
    except ClientError:
        pass

    try:
        nats = ec2.describe_nat_gateways(Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'state', 'Values': ['available', 'pending']}
        ])['NatGateways']
        if nats:
            dependencies.append({
                'type': 'NATGateways', 'count': len(nats),
                'ids': [n['NatGatewayId'] for n in nats],
                'action': 'Delete NAT Gateways before deleting VPC'
            })
    except ClientError:
        pass

    try:
        endpoints = ec2.describe_vpc_endpoints(Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'vpc-endpoint-state', 'Values': ['pending', 'available']}
        ])['VpcEndpoints']
        if endpoints:
            dependencies.append({
                'type': 'VPCEndpoints', 'count': len(endpoints),
                'ids': [e['VpcEndpointId'] for e in endpoints],
                'action': 'Delete VPC Endpoints before deleting VPC'
            })
    except ClientError:
        pass

    try:
        attachments = ec2.describe_transit_gateway_vpc_attachments(Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'state', 'Values': ['available', 'pending', 'modifying']}
        ])['TransitGatewayVpcAttachments']
        if attachments:
            dependencies.append({
                'type': 'TransitGatewayAttachments', 'count': len(attachments),
                'ids': [a['TransitGatewayAttachmentId'] for a in attachments],
                'action': 'Detach from Transit Gateway before deleting VPC'
            })
    except ClientError:
        pass

    return dependencies


def create_vpc_resources(name, cidr_block, region, subnets, tags, created_by):
    ec2 = boto3.client('ec2', region_name=region)

    _step("pre_check_vpc_limit", region=region)
    check_vpc_limit(ec2, region)

    _step("pre_check_cidr_overlap", cidr=cidr_block)
    check_cidr_overlap(ec2, cidr_block)

    _step("pre_check_duplicate_name", vpcName=name)
    check_duplicate_name(name)

    _step("pre_check_availability_zones", subnetCount=len(subnets))
    validate_availability_zones(ec2, subnets, region)

    vpc_id = None
    created_subnets = []
    igw_id = None

    try:
        _step("create_vpc", cidr=cidr_block)
        vpc_response = _with_backoff(
            ec2.create_vpc,
            CidrBlock=cidr_block,
            TagSpecifications=[{'ResourceType': 'vpc', 'Tags': _build_tags(name, tags, created_by)}]
        )
        vpc_id = vpc_response['Vpc']['VpcId']
        _step("create_vpc_done", vpcId=vpc_id)

        _step("modify_vpc_attribute_dns_hostnames", vpcId=vpc_id)
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={'Value': True})

        _step("modify_vpc_attribute_dns_support", vpcId=vpc_id)
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={'Value': True})

        has_public = any(s.get('subnet_type') == 'public' for s in subnets)
        if has_public:
            _step("create_internet_gateway", vpcId=vpc_id)
            igw_id = _create_internet_gateway(ec2, vpc_id, name, tags, created_by)
            _step("create_internet_gateway_done", vpcId=vpc_id, igwId=igw_id)

        subnet_errors = []
        for idx, subnet_config in enumerate(subnets):
            subnet_name = subnet_config.get('name')
            _step("create_subnet", vpcId=vpc_id, index=idx, subnetName=subnet_name,
                  cidr=subnet_config.get('cidr_block'),
                  az=subnet_config.get('availability_zone'),
                  type=subnet_config.get('subnet_type', 'private'))
            try:
                subnet_data = _create_subnet(ec2, vpc_id, subnet_config, igw_id, tags, created_by)
                created_subnets.append(subnet_data)
                _step("create_subnet_done", vpcId=vpc_id, subnetId=subnet_data['subnet_id'])
            except ClientError as e:
                code = e.response['Error']['Code']
                subnet_errors.append({
                    'subnet_name': subnet_name,
                    'cidr_block':  subnet_config.get('cidr_block'),
                    'error_code':  code,
                    'error':       e.response['Error']['Message']
                })
                logger.error("subnet_create_failed", extra={
                    'vpcId': vpc_id, 'subnetName': subnet_name,
                    'errorCode': code, 'errorMessage': e.response['Error']['Message'],
                })
            except ParamValidationError as e:
                # Bad input shape (e.g. missing AvailabilityZone). This is a
                # 400-class problem, not a 500 — surface it as ValidationError.
                logger.warning("subnet_param_validation_failed", extra={
                    'vpcId': vpc_id, 'subnetName': subnet_name, 'error': str(e),
                })
                subnet_errors.append({
                    'subnet_name': subnet_name,
                    'cidr_block':  subnet_config.get('cidr_block'),
                    'error_code':  'ParamValidationError',
                    'error':       str(e),
                })

        if subnet_errors and not created_subnets:
            logger.error("all_subnets_failed_rolling_back", extra={
                'vpcId': vpc_id, 'errors': subnet_errors})
            _rollback_vpc(ec2, vpc_id, [], igw_id)
            raise PartialFailureError(
                message="All subnets failed — VPC rolled back",
                created=[], failed=subnet_errors, vpc_id=None
            )

        vpc_record = {
            'vpc_id': vpc_id, 'name': name, 'cidr_block': cidr_block,
            'region': region, 'subnets': created_subnets,
            'igw_id': igw_id, 'tags': tags, 'created_by': created_by
        }

        try:
            _step("dynamodb_save", vpcId=vpc_id)
            model = VPCModel()
            saved = model.save(vpc_record)
            _step("dynamodb_save_done", vpcId=vpc_id)
        except Exception as db_err:
            logger.critical("dynamodb_save_failed_vpc_orphaned", extra={
                "vpcId": vpc_id,
                "errorType": type(db_err).__name__,
                "errorMessage": str(db_err),
            }, exc_info=True)
            raise PartialFailureError(
                message=f"VPC {vpc_id} created in AWS but metadata storage failed.",
                created=[vpc_id] + [s['subnet_id'] for s in created_subnets],
                failed=["DynamoDB persistence"], vpc_id=vpc_id
            )

        if subnet_errors:
            saved['warnings'] = {
                'partial_failure': True,
                'failed_subnets': subnet_errors,
                'message': f"{len(subnet_errors)} subnet(s) failed"
            }

        return saved

    except (ResourceConflictError, PartialFailureError, AWSThrottlingError, AWSPermissionError, ValidationError):
        raise
    except ParamValidationError as e:
        # Bad args sent to boto3 (usually means the request body is missing
        # a field the code assumed was present). 400, not 500.
        if vpc_id:
            _rollback_vpc(ec2, vpc_id, created_subnets, igw_id)
        logger.warning("param_validation_failed_in_create", extra={"error": str(e)})
        raise ValidationError(f"Invalid request for AWS API: {e}")
    except ClientError as e:
        code = e.response['Error']['Code']
        message = e.response['Error']['Message']
        logger.error("client_error_in_create", extra={
            "vpcId": vpc_id, "errorCode": code, "errorMessage": message,
        })
        if vpc_id:
            _rollback_vpc(ec2, vpc_id, created_subnets, igw_id)
        if code in ('UnauthorizedOperation', 'AccessDenied'):
            raise AWSPermissionError(operation=code, resource=region)
        if code in ('RequestLimitExceeded', 'Throttling'):
            raise AWSThrottlingError("create_vpc")
        raise InfrastructureError(message, aws_error_code=code)


def delete_vpc_resources(vpc_id, region, subnet_ids, deleted_by):
    ec2 = boto3.client('ec2', region_name=region)

    try:
        response = ec2.describe_vpcs(VpcIds=[vpc_id])
        if not response['Vpcs']:
            VPCModel().mark_deleted(vpc_id, deleted_by)
            return
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidVpcID.NotFound':
            VPCModel().mark_deleted(vpc_id, deleted_by)
            return
        raise

    dependencies = check_vpc_dependencies(ec2, vpc_id)
    if dependencies:
        raise DependencyError(vpc_id, dependencies)

    for subnet_id in subnet_ids:
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
        except ClientError as e:
            if e.response['Error']['Code'] != 'InvalidSubnetID.NotFound':
                raise

    rt_response = ec2.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    for rt in rt_response['RouteTables']:
        is_main = any(a.get('Main') for a in rt.get('Associations', []))
        if not is_main:
            for assoc in rt.get('Associations', []):
                if not assoc.get('Main'):
                    ec2.disassociate_route_table(AssociationId=assoc['RouteTableAssociationId'])
            ec2.delete_route_table(RouteTableId=rt['RouteTableId'])

    for igw in ec2.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}])['InternetGateways']:
        ec2.detach_internet_gateway(InternetGatewayId=igw['InternetGatewayId'], VpcId=vpc_id)
        ec2.delete_internet_gateway(InternetGatewayId=igw['InternetGatewayId'])

    ec2.delete_vpc(VpcId=vpc_id)
    VPCModel().mark_deleted(vpc_id, deleted_by)


def _rollback_vpc(ec2, vpc_id, created_subnets, igw_id):
    logger.warning("Rolling back VPC", extra={"vpc_id": vpc_id})
    for subnet in created_subnets:
        try:
            ec2.delete_subnet(SubnetId=subnet['subnet_id'])
        except Exception as e:
            logger.error("Rollback subnet failed", extra={"subnet_id": subnet['subnet_id'], "error": str(e)})
    if igw_id:
        try:
            ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw_id)
        except Exception as e:
            logger.error("Rollback IGW failed", extra={"igw_id": igw_id, "error": str(e)})
    try:
        ec2.delete_vpc(VpcId=vpc_id)
        logger.info("Rollback complete", extra={"vpc_id": vpc_id})
    except Exception as e:
        logger.critical("ROLLBACK FAILED — VPC orphaned", extra={"vpc_id": vpc_id, "error": str(e)})


def _create_subnet(ec2, vpc_id, subnet_config, igw_id, parent_tags, created_by):
    subnet_name = subnet_config['name']
    subnet_type = subnet_config.get('subnet_type', 'private')

    # AvailabilityZone is optional: EC2 will auto-pick one when omitted.
    # But passing AvailabilityZone=None (not omitting it) triggers a boto3
    # ParamValidationError which historically surfaced as a 500. Only include
    # the kwarg when we actually have a value.
    create_kwargs = {
        "VpcId": vpc_id,
        "CidrBlock": subnet_config['cidr_block'],
        "TagSpecifications": [{
            "ResourceType": "subnet",
            "Tags": _build_tags(subnet_name, parent_tags, created_by, {"SubnetType": subnet_type}),
        }],
    }
    az = subnet_config.get('availability_zone')
    if az:
        create_kwargs["AvailabilityZone"] = az

    subnet_id = ec2.create_subnet(**create_kwargs)['Subnet']['SubnetId']

    if subnet_type == 'public':
        ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={'Value': True})
        rt_id = ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[{'ResourceType': 'route-table',
                                'Tags': _build_tags(f"{subnet_name}-rt", parent_tags, created_by)}]
        )['RouteTable']['RouteTableId']
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock='0.0.0.0/0', GatewayId=igw_id)
        ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)

    return {
        'subnet_id': subnet_id, 'name': subnet_name,
        'cidr_block': subnet_config['cidr_block'],
        'availability_zone': subnet_config.get('availability_zone'),
        'subnet_type': subnet_type
    }


def _create_internet_gateway(ec2, vpc_id, vpc_name, tags, created_by):
    igw_id = ec2.create_internet_gateway(
        TagSpecifications=[{'ResourceType': 'internet-gateway',
                            'Tags': _build_tags(f"{vpc_name}-igw", tags, created_by)}]
    )['InternetGateway']['InternetGatewayId']
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    return igw_id


def _build_tags(name, extra_tags, created_by, overrides=None):
    tags = {
        'Name': name, 'CreatedBy': created_by, 'ManagedBy': 'vpc-api',
        'Environment': extra_tags.get('environment', 'unknown'),
        **extra_tags, **(overrides or {})
    }
    return [{'Key': k, 'Value': str(v)} for k, v in tags.items()]
