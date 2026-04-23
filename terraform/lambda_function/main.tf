terraform {
  required_version = ">= 1.6.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.2"
    }
  }

  backend "s3" {
    encrypt  = true
    bucket   = "nayak-manish-terraform-statefiles-va"
    region   = "us-east-1"
    profile  = "manishnayaks-aws"
    key      = "vpc-tools/lambda/terraform.tfstate"
  }
}

provider "aws" {
  region  = "us-east-1"
  profile = "manishnayaks-aws"
}

data "terraform_remote_state" "dynamodb" {
  backend = "s3"

  config = {
    encrypt        = true
    bucket         = "nayak-manish-terraform-statefiles-va"
    region         = "us-east-1"
    profile        = "manishnayaks-aws"
    key            = "vpc-tools/dynamodb/terraform.tfstate"
  }
}


# ── Lambda IAM Role ───────────────────────────────────────────────

resource "aws_iam_role" "lambda_execution" {
  name = "vpc-api-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "vpc-api-lambda-policy"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # CloudWatch Logs — Lambda execution logging
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/vpc-api-*"
      },
      # DynamoDB — VPC metadata storage
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Scan",
          "dynamodb:Query"
        ]
        Resource = "${data.terraform_remote_state.dynamodb.outputs.dynamodb_table_arn}"
      },
      # EC2 — VPC and subnet management
      # Scoped to VPC operations only — not full EC2 admin
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateVpc",
          "ec2:DeleteVpc",
          "ec2:DescribeVpcs",
          "ec2:ModifyVpcAttribute",
          "ec2:CreateSubnet",
          "ec2:DeleteSubnet",
          "ec2:DescribeSubnets",
          "ec2:ModifySubnetAttribute",
          "ec2:CreateInternetGateway",
          "ec2:DeleteInternetGateway",
          "ec2:AttachInternetGateway",
          "ec2:DetachInternetGateway",
          "ec2:DescribeInternetGateways",
          "ec2:CreateRouteTable",
          "ec2:DeleteRouteTable",
          "ec2:CreateRoute",
          "ec2:DeleteRoute",
          "ec2:AssociateRouteTable",
          "ec2:DisassociateRouteTable",
          "ec2:DescribeRouteTables",
          "ec2:CreateTags",
          "ec2:DescribeAvailabilityZones"
        ]
        Resource = "*"  # EC2 VPC actions require * resource
      }
    ]
  })
}

# ── Lambda Package ────────────────────────────────────────────────

data "archive_file" "lambda_package" {
  type        = "zip"
  source_dir  = "${path.module}/../../lambda_source"
  output_path = "${path.module}/lambda_package.zip"
}

# ── Lambda Function ───────────────────────────────────────────────

# Explicit log group so we control retention and the group exists BEFORE the
# function does (prevents the "no log group / no logs visible" 502 dead-end).
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/vpc-api-handler"
  retention_in_days = 14
}

resource "aws_lambda_function" "vpc_api" {
  filename         = data.archive_file.lambda_package.output_path
  function_name    = "vpc-api-handler"
  role             = aws_iam_role.lambda_execution.arn
  # IMPORTANT: handler path must match the zip layout.
  # Zip root = lambda_source/, entry file = vpc_handler.py, function = handler.
  # The old value "handlers.vpc_handler.handler" looked for a non-existent
  # handlers/ package and caused Lambda init to fail → API Gateway 502.
  handler          = "vpc_handler.handler"
  runtime          = "python3.12"
  source_code_hash = data.archive_file.lambda_package.output_base64sha256

  # VPC + subnet + DynamoDB round-trips can easily exceed the default 3s.
  # A too-low timeout causes API Gateway to receive no response → 502.
  timeout     = 30
  memory_size = 512

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = "${data.terraform_remote_state.dynamodb.outputs.dynamodb_table_name}"
      LOG_LEVEL           = "INFO"
    }
  }

  # Enable X-Ray tracing for distributed request tracking
  tracing_config {
    mode = "Active"
  }

  # Make sure the log group is created before the function so the first
  # invocation's logs are captured (otherwise Lambda auto-creates with
  # indefinite retention and we lose control).
  depends_on = [aws_cloudwatch_log_group.lambda_logs]

  tags = {
    Name = "vpc-api-handler"
  }
}

variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  # NOTE: previous default "eu-east-1" is NOT a real AWS region.
  # The provider above pins us to us-east-1, so align the default.
  default     = "us-east-1"
}

output "lambda_function_name" {
  description = "Lambda function name for monitoring"
  value       = aws_lambda_function.vpc_api.function_name
}

output "lambda_invoke_arn" {
  description = "ARN for API Gateway to invoke Lambda"
  value       = aws_lambda_function.vpc_api.invoke_arn
}