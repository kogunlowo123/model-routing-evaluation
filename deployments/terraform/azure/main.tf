terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.5" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
    cognitive_account {
      purge_soft_delete_on_destroy = false
    }
  }
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "routing" {
  name = var.resource_group_name
}

locals {
  name     = var.name
  location = data.azurerm_resource_group.routing.location
  # Key Vault and Cognitive Services names are global; derive a stable unique suffix.
  suffix = substr(sha1("${data.azurerm_client_config.current.subscription_id}${local.name}"), 0, 8)
}

# ------------------------------------------------------------------ database

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "azurerm_postgresql_flexible_server" "routing" {
  name                          = "${local.name}-${local.suffix}"
  resource_group_name           = data.azurerm_resource_group.routing.name
  location                      = local.location
  version                       = "16"
  sku_name                      = var.db_sku
  storage_mb                    = var.db_storage_mb
  delegated_subnet_id           = var.db_subnet_id
  private_dns_zone_id           = var.db_private_dns_zone_id
  public_network_access_enabled = false
  administrator_login           = "routing"
  administrator_password        = random_password.db.result
  backup_retention_days         = 14
  geo_redundant_backup_enabled  = true
  zone                          = "1"
  high_availability {
    mode                      = "ZoneRedundant"
    standby_availability_zone = "2"
  }
  tags = var.tags
}

resource "azurerm_postgresql_flexible_server_configuration" "require_tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.routing.id
  value     = "on"
}

resource "azurerm_postgresql_flexible_server_configuration" "log_connections" {
  name      = "log_connections"
  server_id = azurerm_postgresql_flexible_server.routing.id
  value     = "on"
}

resource "azurerm_postgresql_flexible_server_database" "routing" {
  name      = "routing"
  server_id = azurerm_postgresql_flexible_server.routing.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# ----------------------------------------------------------------- key vault

resource "azurerm_key_vault" "routing" {
  name                          = "kv-${substr(local.name, 0, 12)}-${local.suffix}"
  resource_group_name           = data.azurerm_resource_group.routing.name
  location                      = local.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "premium"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false
  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
  }
  tags = var.tags
}

resource "azurerm_private_endpoint" "key_vault" {
  name                = "${local.name}-kv"
  resource_group_name = data.azurerm_resource_group.routing.name
  location            = local.location
  subnet_id           = var.private_endpoint_subnet_id
  private_service_connection {
    name                           = "${local.name}-kv"
    private_connection_resource_id = azurerm_key_vault.routing.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "vault"
    private_dns_zone_ids = [var.key_vault_private_dns_zone_id]
  }
  tags = var.tags
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "${local.name}-database-url"
  key_vault_id = azurerm_key_vault.routing.id
  value = format(
    "postgresql+asyncpg://routing:%s@%s:5432/routing?ssl=require",
    random_password.db.result, azurerm_postgresql_flexible_server.routing.fqdn,
  )
  content_type = "sqlalchemy-url"
  depends_on   = [azurerm_private_endpoint.key_vault]
}

# ------------------------------------------------------------- Azure OpenAI

resource "azurerm_cognitive_account" "openai" {
  name                          = "${local.name}-oai-${local.suffix}"
  resource_group_name           = data.azurerm_resource_group.routing.name
  location                      = local.location
  kind                          = "OpenAI"
  sku_name                      = "S0"
  custom_subdomain_name         = "${local.name}-${local.suffix}"
  public_network_access_enabled = false
  # The agentic-core Azure OpenAI adapter authenticates with an api-key held
  # in Key Vault and synced by External Secrets.
  local_auth_enabled = true
  network_acls {
    default_action = "Deny"
  }
  identity {
    type = "SystemAssigned"
  }
  tags = var.tags
}

resource "azurerm_cognitive_deployment" "routing" {
  name                 = var.openai_deployment_name
  cognitive_account_id = azurerm_cognitive_account.openai.id
  model {
    format  = "OpenAI"
    name    = var.openai_model_name
    version = var.openai_model_version
  }
  sku {
    # Regional deployment types keep inference in the account's region,
    # which the compliance profile's jurisdiction depends on.
    name     = var.openai_deployment_sku
    capacity = var.openai_capacity
  }
}

resource "azurerm_private_endpoint" "openai" {
  name                = "${local.name}-oai"
  resource_group_name = data.azurerm_resource_group.routing.name
  location            = local.location
  subnet_id           = var.private_endpoint_subnet_id
  private_service_connection {
    name                           = "${local.name}-oai"
    private_connection_resource_id = azurerm_cognitive_account.openai.id
    subresource_names              = ["account"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "openai"
    private_dns_zone_ids = [var.openai_private_dns_zone_id]
  }
  tags = var.tags
}

# ---------------------------------------------- workload identity (AKS)

resource "azurerm_user_assigned_identity" "routing" {
  name                = local.name
  resource_group_name = data.azurerm_resource_group.routing.name
  location            = local.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "routing" {
  name                = "${local.name}-aks"
  resource_group_name = data.azurerm_resource_group.routing.name
  parent_id           = azurerm_user_assigned_identity.routing.id
  issuer              = var.aks_oidc_issuer_url
  subject             = "system:serviceaccount:${var.namespace}:${var.service_account}"
  audience            = ["api://AzureADTokenExchange"]
}

resource "azurerm_role_assignment" "secrets_reader" {
  scope                = azurerm_key_vault.routing.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.routing.principal_id
}

resource "azurerm_role_assignment" "openai_user" {
  scope                = azurerm_cognitive_account.openai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.routing.principal_id
}
