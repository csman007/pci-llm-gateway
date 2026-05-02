# ── Kinesis Firehose: CloudWatch Logs → S3 ────────────────────────────────────

# IAM role assumed by the Firehose delivery stream
resource "aws_iam_role" "firehose" {
  name = "pci-llm-gateway-firehose"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "firehose.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "firehose" {
  name = "pci-llm-gateway-firehose"
  role = aws_iam_role.firehose.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetBucketLocation",
          "s3:ListBucket",
          "s3:AbortMultipartUpload",
          "s3:GetObject",
        ]
        Resource = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = [aws_kms_key.pci.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:PutLogEvents"]
        Resource = [aws_cloudwatch_log_group.firehose.arn]
      }
    ]
  })
}

# IAM role assumed by CloudWatch Logs to write to Firehose
resource "aws_iam_role" "cw_to_firehose" {
  name = "pci-llm-gateway-cw-to-firehose"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
      Condition = {
        StringLike = {
          "aws:SourceArn" = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "cw_to_firehose" {
  name = "pci-llm-gateway-cw-to-firehose"
  role = aws_iam_role.cw_to_firehose.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
      Resource = [aws_kinesis_firehose_delivery_stream.logs.arn]
    }]
  })
}

# CloudWatch log group for Firehose delivery errors
resource "aws_cloudwatch_log_group" "firehose" {
  name              = "/aws/kinesisfirehose/pci-llm-gateway"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.pci.arn
}

resource "aws_cloudwatch_log_stream" "firehose" {
  name           = "DestinationDelivery"
  log_group_name = aws_cloudwatch_log_group.firehose.name
}

# Firehose delivery stream: CloudWatch Logs → S3 (GZIP, partitioned by date)
resource "aws_kinesis_firehose_delivery_stream" "logs" {
  name        = "pci-llm-gateway-logs"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.logs.arn
    prefix              = "cloudwatch-logs/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/"
    compression_format  = "GZIP"
    buffering_interval  = 60
    buffering_size      = 5

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose.name
      log_stream_name = aws_cloudwatch_log_stream.firehose.name
    }
  }
}

# ── CloudWatch Logs subscription filters ───────────────────────────────────────

resource "aws_cloudwatch_log_subscription_filter" "api_gw" {
  name            = "pci-llm-gateway-api-gw-to-s3"
  log_group_name  = aws_cloudwatch_log_group.api_gw.name
  filter_pattern  = ""
  destination_arn = aws_kinesis_firehose_delivery_stream.logs.arn
  role_arn        = aws_iam_role.cw_to_firehose.arn
}

resource "aws_cloudwatch_log_subscription_filter" "lambda" {
  name            = "pci-llm-gateway-lambda-to-s3"
  log_group_name  = aws_cloudwatch_log_group.lambda.name
  filter_pattern  = ""
  destination_arn = aws_kinesis_firehose_delivery_stream.logs.arn
  role_arn        = aws_iam_role.cw_to_firehose.arn
}
