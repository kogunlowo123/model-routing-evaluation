output "workload_identity_client_id" {
  description = "Annotate the service account with azure.workload.identity/client-id = this value"
  value       = azurerm_user_assigned_identity.routing.client_id
}

output "key_vault_uri" {
  description = "Key Vault holding the database URL, provider API keys and audit key"
  value       = azurerm_key_vault.routing.vault_uri
}

output "database_url_secret_name" {
  description = "Key Vault secret synced to Kubernetes as DATABASE_URL"
  value       = azurerm_key_vault_secret.database_url.name
}

output "azure_openai_endpoint" {
  description = "Value for providers.azureOpenAI.endpoint (AZURE_OPENAI_ENDPOINT)"
  value       = azurerm_cognitive_account.openai.endpoint
}

output "azure_openai_deployment" {
  description = "Value for providers.azureOpenAI.deployment (AZURE_OPENAI_DEPLOYMENT)"
  value       = azurerm_cognitive_deployment.routing.name
}

output "region" {
  description = "Region of the Azure OpenAI account; set providers.azureOpenAI.region to it"
  value       = azurerm_cognitive_account.openai.location
}
