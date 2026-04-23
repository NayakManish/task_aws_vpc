"""
Validators — Request payload validation for VPC API.

Validates all incoming request bodies before any AWS API calls.
Returns None on success, error string on failure.

Uses ipaddress module for CIDR validation — no external dependencies.

Author: Platform Engineering
"""

import ipaddress
import re
from typing import Optional


# Valid AWS regions — extend as needed
VALID_REGIONS = {
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
    'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-central-1',
    'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1',
    'ap-south-1', 'sa-east-1', 'ca-central-1'
}

VALID_SUBNET_TYPES = {'public', 'private'}

# RFC 1918 private address spaces only
PRIVATE_CIDR_RANGES = [
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16')
]


def validate_create_vpc_request(body: dict) -> Optional[str]:
    """
    Validate the POST /vpc request body.

    Returns:
        None if valid
        Error message string if invalid
    """
    if not body:
        return "Request body is required"

    # ── Required fields ───────────────────────────────────────────
    required = ['name', 'cidr_block', 'subnets']
    for field in required:
        if field not in body:
            return f"Required field missing: '{field}'"

    # ── Name validation ───────────────────────────────────────────
    name = body['name']
    if not isinstance(name, str) or not name.strip():
        return "Field 'name' must be a non-empty string"

    if len(name) > 64:
        return "Field 'name' must be 64 characters or less"

    if not re.match(r'^[a-zA-Z0-9\-_]+$', name):
        return "Field 'name' must contain only letters, numbers, hyphens, underscores"

    # ── CIDR block validation ─────────────────────────────────────
    cidr_error = _validate_cidr(body['cidr_block'], min_prefix=16, max_prefix=28)
    if cidr_error:
        return f"Invalid VPC cidr_block: {cidr_error}"

    # ── Region validation ─────────────────────────────────────────
    if 'region' in body and body['region'] not in VALID_REGIONS:
        return f"Invalid region '{body['region']}'. Must be one of: {sorted(VALID_REGIONS)}"

    # ── Subnets validation ────────────────────────────────────────
    subnets = body.get('subnets', [])

    if not isinstance(subnets, list):
        return "Field 'subnets' must be a list"

    if len(subnets) == 0:
        return "At least one subnet is required"

    if len(subnets) > 20:
        return "Maximum 20 subnets per VPC"

    vpc_network  = ipaddress.ip_network(body['cidr_block'], strict=False)
    subnet_names = []

    for i, subnet in enumerate(subnets):
        subnet_error = _validate_subnet(subnet, i, vpc_network)
        if subnet_error:
            return subnet_error

        # Check for duplicate subnet names
        subnet_name = subnet.get('name', '')
        if subnet_name in subnet_names:
            return f"Duplicate subnet name: '{subnet_name}'"
        subnet_names.append(subnet_name)

    # ── Tags validation ───────────────────────────────────────────
    if 'tags' in body:
        tags_error = _validate_tags(body['tags'])
        if tags_error:
            return tags_error

    return None  # All validation passed


def _validate_subnet(
    subnet:      dict,
    index:       int,
    vpc_network: ipaddress.IPv4Network
) -> Optional[str]:
    """Validate a single subnet configuration."""
    prefix = f"Subnet[{index}]"

    if not isinstance(subnet, dict):
        return f"{prefix} must be an object"

    # Required subnet fields
    for field in ['name', 'cidr_block']:
        if field not in subnet:
            return f"{prefix}: Required field missing: '{field}'"

    # Subnet name validation
    if not re.match(r'^[a-zA-Z0-9\-_]+$', subnet['name']):
        return f"{prefix}: 'name' must contain only letters, numbers, hyphens, underscores"

    # Subnet CIDR must be within VPC CIDR
    cidr_error = _validate_cidr(
        subnet['cidr_block'], min_prefix=16, max_prefix=28
    )
    if cidr_error:
        return f"{prefix}: Invalid cidr_block: {cidr_error}"

    subnet_network = ipaddress.ip_network(subnet['cidr_block'], strict=False)
    if not subnet_network.subnet_of(vpc_network):
        return (
            f"{prefix}: cidr_block '{subnet['cidr_block']}' "
            f"is not within VPC cidr '{vpc_network}'"
        )

    # Subnet type validation
    subnet_type = subnet.get('subnet_type', 'private')
    if subnet_type not in VALID_SUBNET_TYPES:
        return f"{prefix}: 'subnet_type' must be one of: {VALID_SUBNET_TYPES}"

    # AZ format validation (optional field)
    if 'availability_zone' in subnet:
        az = subnet['availability_zone']
        if not re.match(r'^[a-z]{2}-[a-z]+-\d[a-z]$', az):
            return f"{prefix}: Invalid availability_zone format '{az}' (expected: eu-central-1a)"

    return None


def _validate_cidr(
    cidr:       str,
    min_prefix: int,
    max_prefix: int
) -> Optional[str]:
    """
    Validate CIDR notation.
    Ensures it's a valid private IP range within prefix bounds.
    """
    if not isinstance(cidr, str):
        return "must be a string"

    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return f"'{cidr}' is not valid CIDR notation"

    # Enforce private IP ranges only — no public IPs in VPCs
    is_private = any(
        network.subnet_of(private_range)
        for private_range in PRIVATE_CIDR_RANGES
    )
    if not is_private:
        return f"'{cidr}' must be within a private IP range (RFC 1918)"

    if network.prefixlen < min_prefix:
        return f"Prefix length /{network.prefixlen} is too large (minimum /{min_prefix})"

    if network.prefixlen > max_prefix:
        return f"Prefix length /{network.prefixlen} is too small (maximum /{max_prefix})"

    return None


def _validate_tags(tags: dict) -> Optional[str]:
    """Validate custom tags dict."""
    if not isinstance(tags, dict):
        return "Field 'tags' must be an object"

    if len(tags) > 20:
        return "Maximum 20 custom tags allowed"

    for key, value in tags.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return "Tag keys and values must be strings"
        if len(key) > 128:
            return f"Tag key '{key[:20]}...' exceeds 128 character limit"
        if len(value) > 256:
            return f"Tag value for '{key}' exceeds 256 character limit"

    return None
