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
    key      = "vpc-tools/cognito_user_pool/terraform.tfstate"
  }
}

provider "aws" {
  region  = "us-east-1"
  profile = "manishnayaks-aws"
}

resource "aws_cognito_user_pool" "vpc_api" {
  name = "vpc-api-users"

  # Require email for all users
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Password policy — enterprise grade
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  # MFA — optional but recommended for production
  mfa_configuration = "OPTIONAL"

  tags = {
    Name = "vpc-api-users"
  }
}

output "cognito_user_pool_arn" {
    value       = aws_cognito_user_pool.vpc_api.arn
    description = "ARN of the Cognito User Pool for API authentication"
}