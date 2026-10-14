terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = var.tags }
}

locals {
  name            = var.name
  oidc_issuer     = trimprefix(var.oidc_issuer_url, "https://")
  service_account = "system:serviceaccount:${var.namespace}:${var.service_account}"
  nova_model_arns = [for m in var.bedrock_model_ids : "arn:aws:bedrock:${var.region}::foundation-model/${m}"]
}

# ---------------------------------------------------------------- encryption

resource "aws_kms_key" "routing" {
  description             = "${local.name}: database and secrets encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "routing" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.routing.key_id
}

# ------------------------------------------------------------------ database

resource "aws_db_subnet_group" "routing" {
  name       = local.name
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL access from the Kubernetes cluster only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_cluster" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = var.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Cluster nodes"
}

resource "aws_db_parameter_group" "routing" {
  name   = local.name
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
}

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "aws_db_instance" "routing" {
  identifier                          = local.name
  engine                              = "postgres"
  engine_version                      = "16.4"
  instance_class                      = var.db_instance_class
  allocated_storage                   = var.db_allocated_storage_gb
  max_allocated_storage               = var.db_allocated_storage_gb * 4
  storage_type                        = "gp3"
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.routing.arn
  db_name                             = "routing"
  username                            = "routing"
  password                            = random_password.db.result
  db_subnet_group_name                = aws_db_subnet_group.routing.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  parameter_group_name                = aws_db_parameter_group.routing.name
  multi_az                            = true
  publicly_accessible                 = false
  backup_retention_period             = 14
  deletion_protection                 = true
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name}-final"
  copy_tags_to_snapshot               = true
  auto_minor_version_upgrade          = true
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.routing.arn
  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]
}

resource "aws_secretsmanager_secret" "database_url" {
  name       = "${local.name}-database-url"
  kms_key_id = aws_kms_key.routing.arn
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+asyncpg://routing:%s@%s:%d/routing?ssl=require",
    random_password.db.result, aws_db_instance.routing.address, aws_db_instance.routing.port,
  )
}

# Containers for vendor API keys. Values are written by the platform team
# (aws secretsmanager put-secret-value), never by Terraform.
resource "aws_secretsmanager_secret" "provider_keys" {
  for_each   = toset(["azure-openai", "azure-ai-foundry", "anthropic", "gemini"])
  name       = "${local.name}-${each.key}-api-key"
  kms_key_id = aws_kms_key.routing.arn
}

# ------------------------------------------- workload identity and Bedrock
#
# The role is assumed with a projected service-account token through
# AssumeRoleWithWebIdentity. The OIDC provider may be an EKS cluster (IRSA)
# or an AKS cluster issuer registered as an IAM OIDC provider, which lets an
# Azure-hosted deployment call Nova on Bedrock without static AWS keys.

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:sub"
      values   = [local.service_account]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "routing" {
  name                 = local.name
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "routing" {
  statement {
    sid       = "InvokeApprovedFoundationModels"
    actions   = ["bedrock:InvokeModel"]
    resources = local.nova_model_arns
  }
}

resource "aws_iam_role_policy" "routing" {
  role   = aws_iam_role.routing.id
  policy = data.aws_iam_policy_document.routing.json
}
