# VPC Management API

Serverless REST API for AWS VPC resource creation, retrieval, and deletion. Built with Lambda, API Gateway, DynamoDB, and Cognito.

---

## Architecture & Solution Approach

### Overview
- **Compute:** AWS Lambda (Python 3.12) — stateless, event-driven
- **API:** API Gateway (REST, Cognito-protected) — handles HTTP routing
- **Data:** DynamoDB (on-demand billing) — stores VPC metadata
- **Monitoring:** CloudWatch alarms, X-Ray tracing
- **IaC:** OpenTofu/Terraform — infrastructure as code
- **Auth:** Cognito User Pools — JWT token validation

### Design Principles
- **Separation of Concerns:** Handler → Business Logic → Data Layer
- **Typed Exceptions:** Each failure mode has a specific exception class
- **Validation First:** Input validation before any AWS API calls
- **Soft Deletes:** Mark records as deleted, preserve audit trail
- **Graceful Degradation:** Metric emission failures never break the API
- **Strong Consistency:** DynamoDB uses consistent reads for data accuracy

---

## Repository Structure

```
task_aws_vpc/
├── lambda_source/              # Python Lambda code
│   ├── vpc_handler.py          # HTTP request router & handler
│   ├── vpc_model.py            # DynamoDB data access layer
│   ├── response_model.py       # Standardized API response builder
│   ├── validators.py           # Request payload validation
│   ├── exceptions.py           # Custom exception hierarchy
│   ├── aws_helpers.py          # EC2/VPC resource operations
│   ├── test_validators.py      # Unit tests for validators
│   └── test_negative_scenarios.py  # Integration tests
│
├── terraform/                  # Infrastructure as code (OpenTofu/Terraform)
│   ├── dynamodb/
│   │   └── main.tf            # DynamoDB table (VPC metadata)
│   ├── lambda_function/
│   │   └── main.tf            # Lambda function, IAM role, permissions
│   ├── api-gateway/
│   │   └── main.tf            # API Gateway, routes, integrations
│   ├── cloudwatch/
│   │   └── main.tf            # Alarms, SNS topic, dashboard
│   └── cognito_user_pool/
│       └── main.tf            # Cognito user pool (authentication)
│
└── README.md                   # This file
```

---

## API Specification

### Base URL
```
https://{api-id}.execute-api.{region}.amazonaws.com/Live
```

### Authentication
- All endpoints require Cognito JWT token in `Authorization` header
- Format: `Authorization: Bearer {token}`

### Endpoints

#### 1. Create VPC
**POST** `/vpc`

Request:
```json
{
  "name": "my-vpc",
  "cidr_block": "10.0.0.0/16",
  "region": "eu-central-1",
  "subnets": [
    {
      "name": "public-subnet-1",
      "cidr_block": "10.0.1.0/24",
      "subnet_type": "public",
      "availability_zone": "eu-central-1a"
    },
    {
      "name": "private-subnet-1",
      "cidr_block": "10.0.2.0/24",
      "subnet_type": "private"
    }
  ],
  "tags": {
    "Environment": "staging",
    "Team": "platform"
  }
}
```

Validation Rules:
- `name`: Required, 1-64 chars, alphanumeric + hyphens/underscores
- `cidr_block`: Required, private CIDR (RFC 1918), /16 to /28 prefix
- `region`: Optional (defaults to `eu-central-1`), must be valid AWS region
- `subnets`: Required, 1-20 subnets, must be within VPC CIDR
- `subnet_type`: `public` or `private` (default: private)
- `tags`: Optional, max 20 tags, 128-char keys, 256-char values

Responses:
- **201 Created** — VPC and subnets created successfully
- **207 Multi-Status** — VPC created, some subnets failed (partial failure)
- **400 Bad Request** — Validation error
- **409 Conflict** — VPC name already exists or dependency conflict
- **429 Too Many Requests** — AWS throttled
- **500 Internal Server Error** — Infrastructure error

#### 2. List VPCs
**GET** `/vpc`

Response:
```json
{
  "success": true,
  "data": {
    "vpcs": [
      {
        "vpc_id": "vpc-12345678",
        "name": "my-vpc",
        "cidr_block": "10.0.0.0/16",
        "region": "eu-central-1",
        "status": "active",
        "created_by": "user@example.com",
        "created_at": "2024-04-23T10:30:00Z",
        "subnets": [...]
      }
    ],
    "count": 1
  }
}
```

#### 3. Get VPC by ID
**GET** `/vpc/{vpc_id}`

Response:
```json
{
  "success": true,
  "data": {
    "vpc_id": "vpc-12345678",
    "name": "my-vpc",
    "cidr_block": "10.0.0.0/16",
    "subnets": [...]
  }
}
```

Responses:
- **200 OK** — VPC found
- **404 Not Found** — VPC doesn't exist

#### 4. Delete VPC
**DELETE** `/vpc/{vpc_id}`

Response:
```json
{
  "success": true,
  "data": {
    "message": "VPC vpc-12345678 deleted successfully"
  }
}
```

Notes:
- VPC must have no active dependencies (EC2 instances, RDS, Lambda)
- Record soft-deleted in DynamoDB (status = "deleted")
- Deletion audited with `deleted_by` and `deleted_at` fields

Responses:
- **200 OK** — VPC deleted
- **404 Not Found** — VPC doesn't exist
- **409 Conflict** — VPC has active dependencies

### Standard Response Format
```json
{
  "success": true/false,
  "data": {...} or null,
  "error": "error message" or null,
  "timestamp": "2024-04-23T10:30:00Z"
}
```

### HTTP Status Codes
| Code | Meaning | Exception Type |
|------|---------|---|
| 200 | Success | — |
| 201 | Created | — |
| 207 | Partial Success | PartialFailureError |
| 400 | Bad Request | ValidationError |
| 401 | Unauthorized | — |
| 403 | Forbidden | AWSPermissionError |
| 404 | Not Found | ResourceNotFoundError |
| 409 | Conflict | ResourceConflictError, DependencyError |
| 429 | Too Many Requests | AWSThrottlingError |
| 500 | Internal Error | InfrastructureError |

---

## Data Model

### VPC Record (DynamoDB)
```python
{
  "vpc_id": "vpc-xxxxxxxx",           # Partition Key
  "created_at": "2024-04-23T10:00Z",  # Sort Key
  "name": "my-vpc",
  "cidr_block": "10.0.0.0/16",
  "region": "eu-central-1",
  "status": "active" | "deleted",
  "created_by": "user@example.com",
  "updated_at": "2024-04-23T10:00Z",
  "deleted_at": "2024-04-23T11:00Z",  # Only if deleted
  "deleted_by": "user@example.com",   # Only if deleted
  "tags": {"Environment": "staging"},
  "subnets": [
    {
      "subnet_id": "subnet-xxxxxxxx",
      "name": "public-subnet-1",
      "cidr_block": "10.0.1.0/24",
      "availability_zone": "eu-central-1a",
      "subnet_type": "public",
      "status": "active"
    }
  ]
}
```

### Exception Hierarchy
```
VPCAPIError (base)
├── ValidationError           → 400 Bad Request
├── ResourceNotFoundError     → 404 Not Found
├── ResourceConflictError     → 409 Conflict
├── DependencyError           → 409 Conflict (has dependents)
├── PartialFailureError       → 207 Multi-Status
├── AWSThrottlingError        → 429 Too Many Requests
├── AWSPermissionError        → 403 Forbidden
└── InfrastructureError       → 500 Internal Server Error
```

---

## Prerequisites to Test the API

### AWS Account
- ✅ Active AWS account with credentials configured
- ✅ IAM permissions for EC2, DynamoDB, Lambda, API Gateway, Cognito, CloudWatch
- ✅ S3 bucket for Terraform state files

### Local Tools
- OpenTofu >= 1.6.6 (or Terraform >= 1.6.6)
- AWS CLI v2
- Python 3.12+
- Git

### AWS Configuration
Configure AWS credentials (replace with your AWS profile):
```bash
export AWS_PROFILE=your-profile
aws sts get-caller-identity  # Verify access
```

### Environment Variables
```bash
export AWS_REGION=eu-central-1
export ENVIRONMENT=dev
export DYNAMODB_TABLE_NAME=vpc-api-resources
export LOG_LEVEL=INFO
```

---

## Deployment Steps

### 1. Deploy Infrastructure

Initialize each Terraform module and apply in order:

**DynamoDB:**
```bash
cd terraform/dynamodb
opentofu init -input=false
opentofu plan -out=tfplan
opentofu apply tfplan
```

**Cognito User Pool:**
```bash
cd ../cognito_user_pool
opentofu init -input=false
opentofu plan -out=tfplan
opentofu apply tfplan
```

**Lambda Function:**
```bash
cd ../lambda_function
opentofu init -input=false
opentofu plan -out=tfplan
opentofu apply tfplan
```

**API Gateway:**
```bash
cd ../api-gateway
opentofu init -input=false
opentofu plan -out=tfplan
opentofu apply tfplan
```

**CloudWatch (optional, for monitoring):**
```bash
cd ../cloudwatch
opentofu init -input=false
opentofu plan -out=tfplan
opentofu apply tfplan
```

### 2. Get API Endpoint & Cognito Details

After deployment, retrieve outputs:
```bash
cd terraform/api-gateway
opentofu output api_endpoint
```

For Cognito credentials:
```bash
cd ../cognito_user_pool
opentofu output user_pool_id
opentofu output user_pool_client_id
```

---

## Testing the API

### 1. Create Cognito User (if not using auto signup)
```bash
aws cognito-idp admin-create-user \
  --user-pool-id <user-pool-id> \
  --username testuser \
  --message-action SUPPRESS \
  --temporary-password TempPass123!
```

### 2. Authenticate & Get JWT Token
```bash
AWS_REGION=eu-central-1 aws cognito-idp admin-initiate-auth \
  --user-pool-id <user-pool-id> \
  --client-id <client-id> \
  --auth-flow ADMIN_NO_SRP_AUTH \
  --auth-parameters USERNAME=testuser,PASSWORD=TempPass123!
```

Extract `IdToken` from response.

### 3. Test Create VPC
```bash
curl -X POST https://{api-id}.execute-api.eu-central-1.amazonaws.com/Live/vpc \
  -H "Authorization: Bearer {IdToken}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-vpc",
    "cidr_block": "10.0.0.0/16",
    "subnets": [
      {
        "name": "subnet-1",
        "cidr_block": "10.0.1.0/24",
        "subnet_type": "public"
      }
    ]
  }'
```

### 4. Test List VPCs
```bash
curl https://{api-id}.execute-api.eu-central-1.amazonaws.com/Live/vpc \
  -H "Authorization: Bearer {IdToken}"
```

### 5. Test Get VPC
```bash
curl https://{api-id}.execute-api.eu-central-1.amazonaws.com/Live/vpc/vpc-12345678 \
  -H "Authorization: Bearer {IdToken}"
```

### 6. Test Delete VPC
```bash
curl -X DELETE https://{api-id}.execute-api.eu-central-1.amazonaws.com/Live/vpc/vpc-12345678 \
  -H "Authorization: Bearer {IdToken}"
```

### Run Unit Tests
```bash
cd lambda_source
python -m pytest test_validators.py -v
python -m pytest test_negative_scenarios.py -v
```

---

## Monitoring & Logs

### CloudWatch Logs
Lambda logs: `/aws/lambda/vpc-api-handler`

Filter by request ID:
```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/vpc-api-handler \
  --filter-pattern '{ $.requestId = "req-id-here" }'
```

### CloudWatch Alarms
Dashboard: `https://console.aws.amazon.com/cloudwatch/home#dashboards:name=vpc-api`

Alarms monitored:
- Lambda errors, throttles, duration
- API Gateway 4xx/5xx errors, latency
- DynamoDB read/write throttles, system errors
- Custom metrics: PartialFailure, RollbackTriggered, DependencyBlock, VPCCount

### X-Ray Tracing
View service map and traces in AWS X-Ray console for distributed tracing.

---

## Environment Variables (Lambda)

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DYNAMODB_TABLE_NAME` | Yes | — | DynamoDB table name for VPC metadata |
| `LOG_LEVEL` | No | `INFO` | Logging level (DEBUG, INFO, WARNING) |
| `AWS_REGION` | No | `eu-central-1` | AWS region |
| `ENVIRONMENT` | No | `dev` | Environment name (dev, staging, prod) |

---

## IAM Permissions (Lambda Execution Role)

Lambda role requires:
- **CloudWatch Logs:** CreateLogGroup, CreateLogStream, PutLogEvents
- **DynamoDB:** PutItem, GetItem, UpdateItem, Scan, Query
- **EC2 VPC:** CreateVpc, DeleteVpc, DescribeVpcs, CreateSubnet, DeleteSubnet, etc.

Full policy in `terraform/lambda_function/main.tf`.

---

## Error Handling & Debugging

### Common Issues

**400 Bad Request: "Invalid VPC cidr_block"**
- CIDR must be private (RFC 1918): 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
- Prefix must be /16 to /28 (e.g., 10.0.0.0/16 ✓, 10.0.0.0/30 ✗)

**404 Not Found: "VPC not found"**
- VPC ID doesn't exist in DynamoDB
- VPC may have been deleted (check `status = "deleted"`)

**409 Conflict: "VPC has active dependencies"**
- Check running EC2 instances, RDS, Lambda functions in VPC
- Delete all resources before deleting VPC

**429 Too Many Requests**
- AWS throttled the request
- Retry with exponential backoff (2s, 4s, 8s, etc.)

**500 Internal Server Error**
- Check Lambda logs: `/aws/lambda/vpc-api-handler`
- Verify IAM permissions and DynamoDB table exists

---

## Configuration & Customization

### Change Deployment Region
Update Terraform backend and provider (all modules):
```terraform
provider "aws" {
  region = "us-east-1"  # Change here
}
```

### Change DynamoDB Billing Mode
In `terraform/dynamodb/main.tf`:
```terraform
billing_mode = "PROVISIONED"  # Instead of PAY_PER_REQUEST
# Add: provisioned_throughput { read_capacity_units = 5, write_capacity_units = 5 }
```

### Change API Gateway Stage Name
In `terraform/api-gateway/main.tf`:
```terraform
stage_name = "prod"  # Instead of "Live"
```

---

## Maintenance

### View Terraform State
```bash
cd terraform/dynamodb
opentofu state list
opentofu state show aws_dynamodb_table.vpc_resources
```

### Destroy Infrastructure
```bash
# Destroy in reverse order
cd terraform/cloudwatch && opentofu destroy -auto-approve
cd ../api-gateway && opentofu destroy -auto-approve
cd ../lambda_function && opentofu destroy -auto-approve
cd ../cognito_user_pool && opentofu destroy -auto-approve
cd ../dynamodb && opentofu destroy -auto-approve
```

### Read DynamoDB Table Name from State
```bash
cd terraform/dynamodb
opentofu output dynamodb_table_name
# Output: vpc-api-resources
```

---

## Security Considerations

- ✅ All API calls authenticated via Cognito JWT
- ✅ Lambda runs with least-privilege IAM role (scoped to VPC operations)
- ✅ DynamoDB encrypted at rest (AWS managed keys)
- ✅ Terraform state encrypted in S3 backend
- ✅ No hardcoded credentials (uses IAM roles)
- ✅ Soft deletes preserve audit trail
- ✅ Metric emission failures don't break the API

---

## Support & Troubleshooting

For detailed logs and tracing:
```bash
aws logs tail /aws/lambda/vpc-api-handler --follow
```

For infrastructure debugging:
```bash
opentofu plan -out=tfplan  # Review changes
opentofu show tfplan        # Inspect execution plan
```

---

**Author:** Platform Engineering  
**Last Updated:** April 2024  
**OpenTofu/Terraform Version:** >= 1.6.6  
**Python Version:** 3.12  
**AWS SDK:** boto3
