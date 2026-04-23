"""
Unit Tests — VPC API Validators and Response Models

Tests cover:
    - Valid request payloads
    - Missing required fields
    - Invalid CIDR notation
    - Subnet outside VPC range
    - Duplicate subnet names
    - Invalid region
    - Tag validation

Run with:
    pytest tests/test_validators.py -v

Author: Platform Engineering
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from utils.validators import validate_create_vpc_request


# ── Valid Request Fixtures ─────────────────────────────────────────

def valid_request():
    """Returns a complete valid VPC creation request."""
    return {
        "name":       "test-vpc",
        "cidr_block": "10.0.0.0/16",
        "region":     "eu-central-1",
        "subnets": [
            {
                "name":              "public-subnet-1",
                "cidr_block":        "10.0.1.0/24",
                "availability_zone": "eu-central-1a",
                "subnet_type":       "public"
            },
            {
                "name":              "private-subnet-1",
                "cidr_block":        "10.0.2.0/24",
                "availability_zone": "eu-central-1b",
                "subnet_type":       "private"
            }
        ],
        "tags": {
            "environment": "dev",
            "team":        "platform"
        }
    }


# ── Valid Request Tests ────────────────────────────────────────────

class TestValidRequests:
    def test_valid_full_request(self):
        assert validate_create_vpc_request(valid_request()) is None

    def test_valid_minimal_request(self):
        """Region and tags are optional."""
        request = {
            "name":       "minimal-vpc",
            "cidr_block": "192.168.0.0/24",
            "subnets": [{
                "name":       "subnet-1",
                "cidr_block": "192.168.0.0/26"
            }]
        }
        assert validate_create_vpc_request(request) is None

    def test_valid_172_cidr(self):
        request = valid_request()
        request['cidr_block']          = "172.16.0.0/16"
        request['subnets'][0]['cidr_block'] = "172.16.1.0/24"
        request['subnets'][1]['cidr_block'] = "172.16.2.0/24"
        assert validate_create_vpc_request(request) is None

    def test_valid_private_subnet_only(self):
        request = valid_request()
        for subnet in request['subnets']:
            subnet['subnet_type'] = 'private'
        assert validate_create_vpc_request(request) is None


# ── Missing Field Tests ────────────────────────────────────────────

class TestMissingFields:
    def test_missing_name(self):
        request = valid_request()
        del request['name']
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'name' in error

    def test_missing_cidr_block(self):
        request = valid_request()
        del request['cidr_block']
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'cidr_block' in error

    def test_missing_subnets(self):
        request = valid_request()
        del request['subnets']
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'subnets' in error

    def test_empty_body(self):
        error = validate_create_vpc_request({})
        assert error is not None

    def test_none_body(self):
        error = validate_create_vpc_request(None)
        assert error is not None

    def test_missing_subnet_name(self):
        request = valid_request()
        del request['subnets'][0]['name']
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'name' in error

    def test_missing_subnet_cidr(self):
        request = valid_request()
        del request['subnets'][0]['cidr_block']
        error = validate_create_vpc_request(request)
        assert error is not None


# ── CIDR Validation Tests ──────────────────────────────────────────

class TestCIDRValidation:
    def test_invalid_cidr_format(self):
        request = valid_request()
        request['cidr_block'] = "not-a-cidr"
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'cidr_block' in error

    def test_public_ip_rejected(self):
        """Public IP ranges must be rejected for VPC CIDRs."""
        request = valid_request()
        request['cidr_block'] = "8.8.8.0/24"
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'private' in error.lower()

    def test_subnet_outside_vpc_cidr(self):
        """Subnet CIDR must be within VPC CIDR range."""
        request = valid_request()
        request['subnets'][0]['cidr_block'] = "10.1.0.0/24"  # Outside 10.0.0.0/16
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'within' in error.lower() or 'cidr' in error.lower()

    def test_prefix_too_large(self):
        """VPC prefix smaller than /28 is too small."""
        request = valid_request()
        request['cidr_block']          = "10.0.0.0/29"
        request['subnets'][0]['cidr_block'] = "10.0.0.0/30"
        error = validate_create_vpc_request(request)
        assert error is not None

    def test_prefix_too_small(self):
        """VPC prefix larger than /16 creates too large a network."""
        request = valid_request()
        request['cidr_block']          = "10.0.0.0/8"
        request['subnets'][0]['cidr_block'] = "10.0.1.0/24"
        request['subnets'][1]['cidr_block'] = "10.0.2.0/24"
        error = validate_create_vpc_request(request)
        assert error is not None


# ── Subnet Tests ───────────────────────────────────────────────────

class TestSubnetValidation:
    def test_empty_subnets_list(self):
        request = valid_request()
        request['subnets'] = []
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'subnet' in error.lower()

    def test_duplicate_subnet_names(self):
        request = valid_request()
        request['subnets'][1]['name'] = request['subnets'][0]['name']
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'duplicate' in error.lower()

    def test_invalid_subnet_type(self):
        request = valid_request()
        request['subnets'][0]['subnet_type'] = "dmz"
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'subnet_type' in error

    def test_invalid_az_format(self):
        request = valid_request()
        request['subnets'][0]['availability_zone'] = "invalid-az"
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'availability_zone' in error

    def test_valid_az_format(self):
        request = valid_request()
        request['subnets'][0]['availability_zone'] = "us-east-1a"
        assert validate_create_vpc_request(request) is None

    def test_too_many_subnets(self):
        request = valid_request()
        request['subnets'] = [
            {
                "name":       f"subnet-{i}",
                "cidr_block": f"10.0.{i}.0/24"
            }
            for i in range(25)  # Exceeds limit of 20
        ]
        error = validate_create_vpc_request(request)
        assert error is not None
        assert '20' in error


# ── Region Tests ───────────────────────────────────────────────────

class TestRegionValidation:
    def test_invalid_region(self):
        request = valid_request()
        request['region'] = "xx-invalid-1"
        error = validate_create_vpc_request(request)
        assert error is not None
        assert 'region' in error.lower()

    def test_valid_regions(self):
        valid_regions = ['us-east-1', 'eu-central-1', 'ap-southeast-1']
        for region in valid_regions:
            request = valid_request()
            request['region'] = region
            assert validate_create_vpc_request(request) is None, \
                f"Expected {region} to be valid"


# ── Name Validation Tests ──────────────────────────────────────────

class TestNameValidation:
    def test_name_with_spaces(self):
        request = valid_request()
        request['name'] = "my vpc name"
        error = validate_create_vpc_request(request)
        assert error is not None

    def test_name_too_long(self):
        request = valid_request()
        request['name'] = "a" * 65
        error = validate_create_vpc_request(request)
        assert error is not None
        assert '64' in error

    def test_valid_name_with_hyphens(self):
        request = valid_request()
        request['name'] = "my-test-vpc-01"
        assert validate_create_vpc_request(request) is None

    def test_empty_name(self):
        request = valid_request()
        request['name'] = ""
        error = validate_create_vpc_request(request)
        assert error is not None
