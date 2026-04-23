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
    key      = "vpc-tools/dynamodb/terraform.tfstate"
  }
}

provider "aws" {
  region  = "us-east-1"
  profile = "manishnayaks-aws"
}

resource "aws_dynamodb_table" "vpc_resources" {
  name         = "vpc-api-resources"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "vpc_id"

  attribute {
    name = "vpc_id"
    type = "S"
  }

  # Enable point-in-time recovery — audit trail protection
  point_in_time_recovery {
    enabled = true
  }

  # Encrypt at rest with AWS managed key
  server_side_encryption {
    enabled = true
  }

  tags = {
    Name = "vpc-api-resources"
  }
}

output "dynamodb_table_name" {
  description = "DynamoDB table storing VPC metadata"
  value       = aws_dynamodb_table.vpc_resources.name
}


output "dynamodb_table_arn" {
  description = "DynamoDB table storing VPC metadata"
  value       = aws_dynamodb_table.vpc_resources.arn
}