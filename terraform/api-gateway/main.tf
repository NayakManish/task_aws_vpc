terraform {
  required_version = ">= 1.6.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    encrypt  = true
    bucket   = "nayak-manish-terraform-statefiles-va"
    region   = "us-east-1"
    profile  = "manishnayaks-aws"
    key      = "vpc-tools/api-gateway/terraform.tfstate"
  }
}

provider "aws" {
  region  = "us-east-1"
  profile = "manishnayaks-aws"
}

data "terraform_remote_state" "lambda" {
  backend = "s3"

  config = {
    encrypt        = true
    bucket         = "nayak-manish-terraform-statefiles-va"
    region         = "us-east-1"
    profile        = "manishnayaks-aws"
    key            = "vpc-tools/lambda/terraform.tfstate"
  }
}

data "terraform_remote_state" "cognito_user_pool" {
  backend = "s3"

  config = {
    encrypt        = true
    bucket         = "nayak-manish-terraform-statefiles-va"
    region         = "us-east-1"
    profile        = "manishnayaks-aws"
    key            = "vpc-tools/cognito_user_pool/terraform.tfstate"
  }
}

// Allow API Gateway to push logs to CloudWatch. API Gateway requires an
// account-level CloudWatch role to be set before stage-level logging can be
// enabled. This creates a role trusted by API Gateway and attaches the
// managed policy that grants the necessary CloudWatch permissions.
resource "aws_iam_role" "api_gateway_cloudwatch_role" {
  name = "apigateway-cloudwatch-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch_role_attach" {
  role       = aws_iam_role.api_gateway_cloudwatch_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

// Set the account-level CloudWatch role for API Gateway. This is a singleton
// resource per account/region and must be present for stage logging to work.
resource "aws_api_gateway_account" "account" {
  cloudwatch_role_arn = aws_iam_role.api_gateway_cloudwatch_role.arn
}


resource "aws_api_gateway_rest_api" "vpc_api" {
  name        = "vpc-api"
  description = "VPC Management API — creates and retrieves VPC resources"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = {
    Name = "vpc-api"
  }
}

# Cognito Authorizer — validates JWT tokens
resource "aws_api_gateway_authorizer" "cognito" {
  name            = "cognito-authorizer"
  rest_api_id     = aws_api_gateway_rest_api.vpc_api.id
  type            = "COGNITO_USER_POOLS"
  provider_arns = [data.terraform_remote_state.cognito_user_pool.outputs.cognito_user_pool_arn]
  identity_source = "method.request.header.Authorization"
}

# /vpc resource
resource "aws_api_gateway_resource" "vpc" {
  rest_api_id = aws_api_gateway_rest_api.vpc_api.id
  parent_id   = aws_api_gateway_rest_api.vpc_api.root_resource_id
  path_part   = "vpc"
}

# /vpc/{vpc_id} resource
resource "aws_api_gateway_resource" "vpc_id" {
  rest_api_id = aws_api_gateway_rest_api.vpc_api.id
  parent_id   = aws_api_gateway_resource.vpc.id
  path_part   = "{vpc_id}"
}

# Helper to create methods with Cognito auth
locals {
  methods = {
    "POST-vpc"       = { resource_id = aws_api_gateway_resource.vpc.id,    http_method = "POST" }
    "GET-vpc"        = { resource_id = aws_api_gateway_resource.vpc.id,    http_method = "GET" }
    "GET-vpc-id"     = { resource_id = aws_api_gateway_resource.vpc_id.id, http_method = "GET" }
    "DELETE-vpc-id"  = { resource_id = aws_api_gateway_resource.vpc_id.id, http_method = "DELETE" }
  }
}

resource "aws_api_gateway_method" "methods" {
  for_each = local.methods

  rest_api_id   = aws_api_gateway_rest_api.vpc_api.id
  resource_id   = each.value.resource_id
  http_method   = each.value.http_method
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}

resource "aws_api_gateway_integration" "integrations" {
  for_each = local.methods

  rest_api_id             = aws_api_gateway_rest_api.vpc_api.id
  resource_id             = each.value.resource_id
  http_method             = aws_api_gateway_method.methods[each.key].http_method
  integration_http_method = "POST"  # Lambda always uses POST
  type                    = "AWS_PROXY"
  uri                     = data.terraform_remote_state.lambda.outputs.lambda_invoke_arn
}

# Lambda permission for API Gateway invocation
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = data.terraform_remote_state.lambda.outputs.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.vpc_api.execution_arn}/*/*"
}

# API Deployment
resource "aws_api_gateway_deployment" "vpc_api" {
  rest_api_id = aws_api_gateway_rest_api.vpc_api.id

  # Force new deployment when methods change
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_method.methods,
      aws_api_gateway_integration.integrations
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_method.methods,
    aws_api_gateway_integration.integrations
  ]
}

resource "aws_api_gateway_stage" "vpc_api" {
  deployment_id = aws_api_gateway_deployment.vpc_api.id
  rest_api_id   = aws_api_gateway_rest_api.vpc_api.id
  stage_name    = "Live"

  # Access logs — one line per request (who, what, status, latency).
  # Extended with the integration status + Lambda error + latency fields,
  # which are the single most useful signals for 502 triage: integrationStatus
  # tells you what Lambda actually returned to the gateway, integrationLatency
  # tells you whether Lambda timed out, and integrationErrorMessage surfaces
  # the raw reason API Gateway rejected the Lambda response.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway_logs.arn
    format          = "{\"requestId\":\"$context.requestId\",\"extendedRequestId\":\"$context.extendedRequestId\",\"ip\":\"$context.identity.sourceIp\",\"user\":\"$context.identity.user\",\"requestTime\":\"$context.requestTime\",\"httpMethod\":\"$context.httpMethod\",\"resourcePath\":\"$context.resourcePath\",\"path\":\"$context.path\",\"status\":\"$context.status\",\"protocol\":\"$context.protocol\",\"responseLength\":\"$context.responseLength\",\"responseLatency\":\"$context.responseLatency\",\"integrationStatus\":\"$context.integrationStatus\",\"integrationLatency\":\"$context.integrationLatency\",\"integrationErrorMessage\":\"$context.integrationErrorMessage\",\"authorizerError\":\"$context.authorizer.error\"}"
  }

  # Enable X-Ray tracing
  xray_tracing_enabled = true

  depends_on = [
    aws_api_gateway_account.account
  ]
}

# Execution-level logging — full request/response bodies sent to & received
# from the Lambda integration. This is what actually reveals "Lambda returned
# a malformed response" vs "Lambda timed out" vs "Lambda threw on init".
# INFO + data_trace_enabled is verbose; keep it on while debugging 502s and
# dial back to ERROR once the root cause is found.
resource "aws_api_gateway_method_settings" "vpc_api_all" {
  rest_api_id = aws_api_gateway_rest_api.vpc_api.id
  stage_name  = aws_api_gateway_stage.vpc_api.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled      = true
    logging_level        = "INFO"
    data_trace_enabled   = true   # logs full request/response bodies — dev only
    throttling_rate_limit  = 100
    throttling_burst_limit = 50
  }
}

resource "aws_cloudwatch_log_group" "api_gateway_logs" {
  name              = "/aws/api-gateway/vpc-api"
  retention_in_days = 14
}


output "api_endpoint" {
  description = "API Gateway base URL"
  value       = "${aws_api_gateway_stage.vpc_api.invoke_url}"
}

output "api_name" {
  description = "API Gateway name"
  value       = aws_api_gateway_rest_api.vpc_api.name
}