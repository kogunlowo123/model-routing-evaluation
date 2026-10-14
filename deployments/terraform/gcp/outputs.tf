output "gcp_service_account" {
  description = "Annotate the service account with iam.gke.io/gcp-service-account = this value"
  value       = google_service_account.routing.email
}

output "database_url_secret" {
  description = "Secret Manager secret synced to Kubernetes as DATABASE_URL"
  value       = google_secret_manager_secret.database_url.secret_id
}

output "provider_key_secrets" {
  description = "Secret Manager secrets that hold vendor API keys"
  value       = { for k, s in google_secret_manager_secret.provider_keys : k => s.secret_id }
}
