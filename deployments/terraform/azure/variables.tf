variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "model-routing-evaluation"
}

variable "subscription_id" {
  description = "Azure subscription"
  type        = string
}

variable "resource_group_name" {
  description = "Existing resource group"
  type        = string
}

variable "db_subnet_id" {
  description = "Subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers"
  type        = string
}

variable "db_private_dns_zone_id" {
  description = "Private DNS zone privatelink.postgres.database.azure.com linked to the VNet"
  type        = string
}

variable "private_endpoint_subnet_id" {
  description = "Subnet for the Key Vault and Azure OpenAI private endpoints"
  type        = string
}

variable "key_vault_private_dns_zone_id" {
  description = "Private DNS zone privatelink.vaultcore.azure.net linked to the VNet"
  type        = string
}

variable "openai_private_dns_zone_id" {
  description = "Private DNS zone privatelink.openai.azure.com linked to the VNet"
  type        = string
}

variable "db_sku" {
  description = "PostgreSQL Flexible Server SKU"
  type        = string
  default     = "GP_Standard_D4ds_v5"
}

variable "db_storage_mb" {
  description = "Database storage"
  type        = number
  default     = 65536
}

variable "openai_deployment_name" {
  description = "Azure OpenAI deployment name; becomes AZURE_OPENAI_DEPLOYMENT"
  type        = string
  default     = "routing-primary"
}

variable "openai_model_name" {
  description = "Azure OpenAI model to deploy, as listed for the account's region"
  type        = string
}

variable "openai_model_version" {
  description = "Model version available in the account's region"
  type        = string
}

variable "openai_deployment_sku" {
  description = "Deployment type; Standard and ProvisionedManaged keep processing in-region"
  type        = string
  default     = "Standard"
  validation {
    condition     = contains(["Standard", "ProvisionedManaged", "DataZoneStandard", "GlobalStandard"], var.openai_deployment_sku)
    error_message = "Unsupported deployment type."
  }
}

variable "openai_capacity" {
  description = "Deployment capacity in thousands of tokens per minute"
  type        = number
  default     = 10
}

variable "aks_oidc_issuer_url" {
  description = "AKS cluster OIDC issuer URL (az aks show --query oidcIssuerProfile.issuerUrl)"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace of the service"
  type        = string
  default     = "model-routing"
}

variable "service_account" {
  description = "Kubernetes service account of the service"
  type        = string
  default     = "model-routing-evaluation"
}

variable "tags" {
  description = "Tags applied to every resource"
  type        = map(string)
  default     = { "app.kubernetes.io/part-of" = "model-routing-evaluation" }
}
