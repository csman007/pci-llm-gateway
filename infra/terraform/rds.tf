# ── RDS PostgreSQL with pgvector ──────────────────────────────────────────────
# pgvector is available on RDS PostgreSQL 15+ via CREATE EXTENSION (no parameter
# group change required). The extension is created by the ingestion script on
# first run via VectorStore.initialise().

resource "aws_db_subnet_group" "main" {
  name       = "pci-llm-gateway"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "pci-llm-gateway" }
}

resource "aws_security_group" "rds" {
  name        = "pci-llm-gateway-rds"
  description = "Allow PostgreSQL inbound from Lambda SG only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.gateway.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"] #tfsec:ignore:aws-ec2-no-public-egress-sgr -- egress only; no public ingress on this SG
  }

  tags = { Name = "pci-llm-gateway-rds" }
}

resource "aws_db_instance" "pgvector" {
  identifier        = "pci-llm-gateway-pgvector"
  engine            = "postgres"
  engine_version    = "16"
  instance_class    = var.rds_instance_class
  allocated_storage = var.rds_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true
  kms_key_id        = aws_kms_key.main.arn

  db_name  = "pci_gateway"
  username = "gateway"
  password = var.rds_password

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  multi_az               = true
  publicly_accessible    = false
  deletion_protection    = true
  skip_final_snapshot    = false
  final_snapshot_identifier = "pci-llm-gateway-pgvector-final"

  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "mon:04:00-mon:05:00"

  performance_insights_enabled = true
  monitoring_interval          = 60

  tags = { Name = "pci-llm-gateway-pgvector" }
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint (host:port)"
  value       = aws_db_instance.pgvector.endpoint
}
