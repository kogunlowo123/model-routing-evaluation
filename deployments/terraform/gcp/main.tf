terraform {
  required_version = ">= 1.7.0"
  required_providers {
    google      = { source = "hashicorp/google", version = "~> 6.8" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 6.8" }
    random      = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  name = var.name
}

# ---------------------------------------------------------------- encryption

resource "google_kms_key_ring" "routing" {
  name     = local.name
  location = var.region
}

resource "google_kms_crypto_key" "routing" {
  name            = "${local.name}-data"
  key_ring        = google_kms_key_ring.routing.id
  rotation_period = "7776000s" # 90 days
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_service_identity" "sql" {
  provider = google-beta
  project  = var.project_id
  service  = "sqladmin.googleapis.com"
}

resource "google_project_service_identity" "secret_manager" {
  provider = google-beta
  project  = var.project_id
  service  = "secretmanager.googleapis.com"
}

resource "google_kms_crypto_key_iam_member" "sql" {
  crypto_key_id = google_kms_crypto_key.routing.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.sql.email}"
}

resource "google_kms_crypto_key_iam_member" "secret_manager" {
  crypto_key_id = google_kms_crypto_key.routing.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.secret_manager.email}"
}

# ------------------------------------------------------------------ database

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "google_sql_database_instance" "routing" {
  name                = local.name
  database_version    = "POSTGRES_16"
  region              = var.region
  encryption_key_name = google_kms_crypto_key.routing.id
  deletion_protection = true
  settings {
    tier              = var.db_tier
    availability_type = "REGIONAL"
    disk_autoresize   = true
    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = var.network_id
      ssl_mode                                      = "ENCRYPTED_ONLY"
      enable_private_path_for_google_cloud_services = true
    }
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      backup_retention_settings {
        retained_backups = 14
      }
    }
    database_flags {
      name  = "log_connections"
      value = "on"
    }
    database_flags {
      name  = "log_disconnections"
      value = "on"
    }
    database_flags {
      name  = "log_checkpoints"
      value = "on"
    }
    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }
    database_flags {
      name  = "log_temp_files"
      value = "0"
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.sql]
}

resource "google_sql_database" "routing" {
  name     = "routing"
  instance = google_sql_database_instance.routing.name
}

resource "google_sql_user" "routing" {
  name     = "routing"
  instance = google_sql_database_instance.routing.name
  password = random_password.db.result
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = "${local.name}-database-url"
  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.routing.id
        }
      }
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.secret_manager]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  secret_data = format(
    "postgresql+asyncpg://routing:%s@%s:5432/routing?ssl=require",
    random_password.db.result, google_sql_database_instance.routing.private_ip_address,
  )
}

# Containers for vendor API keys. Values are added by the platform team
# (gcloud secrets versions add), never by Terraform.
resource "google_secret_manager_secret" "provider_keys" {
  for_each  = toset(["azure-openai", "azure-ai-foundry", "anthropic", "gemini"])
  secret_id = "${local.name}-${each.key}-api-key"
  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.routing.id
        }
      }
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.secret_manager]
}

# --------------------------------------------- workload identity (GKE)

resource "google_service_account" "routing" {
  account_id   = substr(local.name, 0, 30)
  display_name = "Model routing and evaluation"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.routing.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.service_account}]"
}

resource "google_secret_manager_secret_iam_member" "database_url" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.routing.email}"
}

resource "google_secret_manager_secret_iam_member" "provider_keys" {
  for_each  = google_secret_manager_secret.provider_keys
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.routing.email}"
}
