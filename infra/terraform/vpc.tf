locals {
  az_count = max(2, length(data.aws_availability_zones.available.names))
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "pci-llm-gateway-vpc" }
}

resource "aws_subnet" "private" {
  count             = local.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "pci-llm-gateway-private-${data.aws_availability_zones.available.names[count.index]}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pci-llm-gateway-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = local.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "gateway" {
  name        = "pci-llm-gateway-sg"
  description = "Lambda security group — no public ingress, HTTPS-only egress"
  vpc_id      = aws_vpc.main.id

  # Intra-group only — covers Lambda ↔ PrivateLink endpoint ENI communication.
  ingress {
    description = "Intra-group HTTPS for PrivateLink endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    self        = true
  }

  # HTTPS only — Anthropic, OpenAI, and Cognito JWKS.
  # DynamoDB and Secrets Manager route through PrivateLink endpoints below.
  # LLM provider IPs are not stable CIDRs; HTTPS-only port limits blast radius.
  egress {
    description = "HTTPS to external APIs (LLM providers + Cognito JWKS)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] #tfsec:ignore:aws-ec2-no-public-egress-sgr
  }

  tags = { Name = "pci-llm-gateway-sg" }
}

# ── VPC Endpoints ─────────────────────────────────────────────────────────

# DynamoDB — Gateway endpoint (free). Traffic stays on the AWS backbone,
# never reaches the internet. PCI-acceptable without PrivateLink cost.
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "pci-llm-gateway-dynamodb-endpoint" }
}

# Secrets Manager — PrivateLink (Interface). No gateway option exists;
# Interface endpoint keeps secrets traffic on private IPs across all AZs.
resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.gateway.id]
  private_dns_enabled = true

  tags = { Name = "pci-llm-gateway-secretsmanager-privatelink" }
}

# ── VPC Flow Logs ──────────────────────────────────────────────────────────

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "pci-llm-gateway-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
}

data "aws_iam_policy_document" "flow_logs_s3" {
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/vpc-flow-logs/*"] #tfsec:ignore:aws-iam-no-policy-wildcards -- path prefix, not a permission wildcard
  }
}

resource "aws_iam_role_policy" "flow_logs_s3" {
  name   = "pci-llm-gateway-flow-logs-s3"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs_s3.json
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL"
  iam_role_arn    = aws_iam_role.flow_logs.arn
  log_destination_type = "s3"
  log_destination = "${aws_s3_bucket.logs.arn}/vpc-flow-logs/"

  tags = { Name = "pci-llm-gateway-flow-logs" }
}
