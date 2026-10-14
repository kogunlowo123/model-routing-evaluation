output "service_role_arn" {
  description = "providers.bedrock.roleArn (cross-cloud) or eks.amazonaws.com/role-arn annotation (EKS)"
  value       = aws_iam_role.routing.arn
}

output "database_url_secret_name" {
  description = "Secrets Manager secret synced to Kubernetes as DATABASE_URL"
  value       = aws_secretsmanager_secret.database_url.name
}

output "provider_key_secret_names" {
  description = "Secrets Manager secrets that hold vendor API keys"
  value       = { for k, s in aws_secretsmanager_secret.provider_keys : k => s.name }
}

output "kms_key_arn" {
  description = "KMS key encrypting the database and secrets; use it for the audit-key secret"
  value       = aws_kms_key.routing.arn
}
