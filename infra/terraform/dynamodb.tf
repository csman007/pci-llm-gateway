resource "aws_dynamodb_table" "tenants" {
  name         = var.tenants_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.pci.arn
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Name = var.tenants_table }
}

resource "aws_dynamodb_table" "rate_limit" {
  name         = var.rate_limit_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.pci.arn
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Name = var.rate_limit_table }
}

resource "aws_dynamodb_table" "audit_log" {
  name         = "pci-llm-gateway-audit"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"
  range_key    = "timestamp"

  attribute {
    name = "request_id"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.pci.arn
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = { Name = "pci-llm-gateway-audit" }
}
