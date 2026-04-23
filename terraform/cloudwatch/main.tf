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

data "terraform_remote_state" "api-gateway" {
  backend = "s3"

  config = {
    encrypt        = true
    bucket         = "nayak-manish-terraform-statefiles-va"
    region         = "us-east-1"
    profile        = "manishnayaks-aws"
    key            = "vpc-tools/api-gateway/terraform.tfstate"
  }
}


resource "aws_sns_topic" "vpc_api_alarms" {
  name = "vpc-api-alarms"

  tags = {
    Name = "vpc-api-alarms"
  }
}

resource "aws_sns_topic_subscription" "email_alert" {
  topic_arn = aws_sns_topic.vpc_api_alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

variable "alert_email" {
  description = "Email address to receive CloudWatch alarm notifications"
  type        = string
  default     = "itsmanishnayak@gmail.com"
}

# ── Lambda Alarms ─────────────────────────────────────────────────

# 1. Lambda errors — any unhandled exception or timeout
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "vpc-api-lambda-errors"
  alarm_description   = "Lambda function throwing errors — check CloudWatch logs"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    # FunctionName = aws_lambda_function.vpc_api.function_name
    FunctionName = data.terraform_remote_state.lambda.outputs.lambda_function_name
  }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
  ok_actions          = [aws_sns_topic.vpc_api_alarms.arn]
}

# 2. Lambda throttles — function being rate-limited by AWS
resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "vpc-api-lambda-throttles"
  alarm_description   = "Lambda being throttled — concurrent execution limit hit"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions = {
    FunctionName =data.terraform_remote_state.lambda.outputs.lambda_function_name
  }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 3. Lambda duration approaching timeout
# Alarm at 80% of configured timeout
resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  alarm_name          = "vpc-api-lambda-duration"
  alarm_description   = "Lambda execution time approaching timeout limit (${var.lambda_timeout}s)"
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions = {
    FunctionName = data.terraform_remote_state.lambda.outputs.lambda_function_name
  }
  extended_statistic  = "p99"
  period              = 300
  evaluation_periods  = 2
  # Alert at 80% of timeout — gives time to investigate before hard timeout
  threshold           = var.lambda_timeout * 1000 * 0.8
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 4. Lambda no invocations — API has gone silent
# During business hours, zero invocations for 1 hour is suspicious
resource "aws_cloudwatch_metric_alarm" "lambda_no_invocations" {
  alarm_name          = "vpc-api-lambda-no-invocations"
  alarm_description   = "No Lambda invocations in 1 hour — API may be down or unreachable"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions = {
    FunctionName = data.terraform_remote_state.lambda.outputs.lambda_function_name
  }
  statistic           = "Sum"
  period              = 3600   # 1 hour
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # Only alarm if we EXPECT traffic — ignore in dev
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# ── API Gateway Alarms ────────────────────────────────────────────

# 5. API Gateway 4xx error rate > 10% over 5 minutes
resource "aws_cloudwatch_metric_alarm" "apigw_4xx_rate" {
  alarm_name          = "vpc-api-gateway-4xx-rate"
  alarm_description   = "High 4xx error rate — auth failures, bad requests, or client issues"
  namespace           = "AWS/ApiGateway"
  metric_name         = "4XXError"
  dimensions = {
    ApiName  = data.terraform_remote_state.api-gateway.id
  }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 0.1   # 10% error rate
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 6. API Gateway 5xx errors — server-side failures
resource "aws_cloudwatch_metric_alarm" "apigw_5xx_errors" {
  alarm_name          = "vpc-api-gateway-5xx-errors"
  alarm_description   = "API Gateway 5xx errors — Lambda failures or integration issues"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  dimensions = {
    ApiName  = data.terraform_remote_state.api-gateway.id
  }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
  ok_actions          = [aws_sns_topic.vpc_api_alarms.arn]
}

# 7. API Gateway p99 latency > 10s
resource "aws_cloudwatch_metric_alarm" "apigw_latency" {
  alarm_name          = "vpc-api-gateway-latency"
  alarm_description   = "API Gateway p99 latency > 10s — VPC creation taking too long"
  namespace           = "AWS/ApiGateway"
  metric_name         = "Latency"
  dimensions = {
    ApiName  = data.terraform_remote_state.api-gateway.id
  }
  extended_statistic  = "p99"
  period              = 300
  evaluation_periods  = 2
  threshold           = 10000  # 10 seconds in milliseconds
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# ── DynamoDB Alarms ───────────────────────────────────────────────

# 8. DynamoDB read throttle
resource "aws_cloudwatch_metric_alarm" "dynamodb_read_throttle" {
  alarm_name          = "vpc-api-dynamodb-read-throttle"
  alarm_description   = "DynamoDB read requests being throttled — list/get operations failing"
  namespace           = "AWS/DynamoDB"
  metric_name         = "ReadThrottleEvents"
  dimensions = {
    TableName = data.terraform_remote_state.dynamodb.outputs.dynamodb_table_name
  }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 9. DynamoDB write throttle
resource "aws_cloudwatch_metric_alarm" "dynamodb_write_throttle" {
  alarm_name          = "vpc-api-dynamodb-write-throttle"
  alarm_description   = "DynamoDB write requests throttled — VPC metadata not being saved"
  namespace           = "AWS/DynamoDB"
  metric_name         = "WriteThrottleEvents"
  dimensions = {
    TableName = data.terraform_remote_state.dynamodb.outputs.dynamodb_table_name
  }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 10. DynamoDB system errors
resource "aws_cloudwatch_metric_alarm" "dynamodb_system_errors" {
  alarm_name          = "vpc-api-dynamodb-system-errors"
  alarm_description   = "DynamoDB system errors — AWS-side issues affecting metadata storage"
  namespace           = "AWS/DynamoDB"
  metric_name         = "SystemErrors"
  dimensions = {
    TableName = data.terraform_remote_state.dynamodb.outputs.dynamodb_table_name
  }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# ── Custom Metric Alarms (application-level events) ───────────────
# These use custom CloudWatch metrics emitted by Lambda
# See: cloudwatch_metrics.py for the metric emission code

# 11. Partial failure — VPC created but some subnets failed
resource "aws_cloudwatch_metric_alarm" "partial_failure" {
  alarm_name          = "vpc-api-partial-failure"
  alarm_description   = "VPC creation partial failure — some subnets failed. Manual review required."
  namespace           = "VPCApi/Operations"
  metric_name         = "PartialFailure"
  # dimensions = {
  #   Environment = var.environment
  # }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 12. Rollback triggered — VPC creation failed and was rolled back
resource "aws_cloudwatch_metric_alarm" "rollback_triggered" {
  alarm_name          = "vpc-api-rollback-triggered"
  alarm_description   = "VPC creation rollback triggered — all subnets failed, VPC deleted"
  namespace           = "VPCApi/Operations"
  metric_name         = "RollbackTriggered"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 13. Delete blocked by dependencies
resource "aws_cloudwatch_metric_alarm" "dependency_block" {
  alarm_name          = "vpc-api-dependency-block"
  alarm_description   = "VPC deletion blocked by active resources — user needs to clean up first"
  namespace           = "VPCApi/Operations"
  metric_name         = "DependencyBlock"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3   # Alert if 3+ blocked in 5 min — unusual pattern
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# 14. VPC limit approaching (> 80% of limit)
resource "aws_cloudwatch_metric_alarm" "vpc_limit_warning" {
  alarm_name          = "vpc-api-vpc-limit-warning"
  alarm_description   = "VPC count approaching regional limit — request limit increase proactively"
  namespace           = "VPCApi/Operations"
  metric_name         = "VPCCount"
  dimensions = {
    Region      = var.aws_region
  }
  statistic           = "Maximum"
  period              = 3600   # Check hourly
  evaluation_periods  = 1
  threshold           = 4      # Alert at 4/5 (80% of default limit)
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.vpc_api_alarms.arn]
}

# ── CloudWatch Dashboard ──────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "vpc_api" {
  dashboard_name = "vpc-api"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Lambda — Invocations, Errors, Throttles"
          view   = "timeSeries"
          period = 60
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", data.terraform_remote_state.lambda.outputs.lambda_function_name, {stat="Sum", color="#2ca02c"}],
            ["AWS/Lambda", "Errors",      "FunctionName", data.terraform_remote_state.lambda.outputs.lambda_function_name, {stat="Sum", color="#d62728"}],
            ["AWS/Lambda", "Throttles",   "FunctionName", data.terraform_remote_state.lambda.outputs.lambda_function_name, {stat="Sum", color="#ff7f0e"}]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "API Gateway — 4xx, 5xx, Latency p99"
          view   = "timeSeries"
          period = 60
          metrics = [
            ["AWS/ApiGateway", "4XXError", "ApiName", data.terraform_remote_state.api-gateway.id, {stat="Sum", color="#ff7f0e"}],
            ["AWS/ApiGateway", "5XXError", "ApiName", data.terraform_remote_state.api-gateway.id, {stat="Sum", color="#d62728"}],
            ["AWS/ApiGateway", "Latency",  "ApiName", data.terraform_remote_state.api-gateway.id, {stat="p99",  yAxis="right"}]
          ]
        }
      },
      {
        type   = "metric"
        x      = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "DynamoDB — Throttles and System Errors"
          view   = "timeSeries"
          period = 60
          metrics = [
            ["AWS/DynamoDB", "ReadThrottleEvents",  "TableName", data.terraform_remote_state.dynamodb.outputs.dynamodb_table_name, {stat="Sum"}],
            ["AWS/DynamoDB", "WriteThrottleEvents", "TableName", data.terraform_remote_state.dynamodb.outputs.dynamodb_table_name, {stat="Sum"}],
            ["AWS/DynamoDB", "SystemErrors",        "TableName", data.terraform_remote_state.dynamodb.outputs.dynamodb_table_name, {stat="Sum", color="#d62728"}]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Application Events — Partial Failures, Rollbacks, Dependency Blocks"
          view   = "timeSeries"
          period = 300
          metrics = [
            ["VPCApi/Operations", "PartialFailure",    {stat="Sum", color="#ff7f0e"}],
            ["VPCApi/Operations", "RollbackTriggered", {stat="Sum", color="#d62728"}],
            ["VPCApi/Operations", "DependencyBlock",   {stat="Sum", color="#9467bd"}],
            ["VPCApi/Operations", "VPCCount", "Region", var.aws_region, {stat="Maximum", yAxis="right"}]
          ]
        }
      }
    ]
  })
}
