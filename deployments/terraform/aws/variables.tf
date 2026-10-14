variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "model-routing-evaluation"
}

variable "region" {
  description = "AWS region; Bedrock calls and data stay in this region"
  type        = string
}

variable "vpc_id" {
  description = "VPC hosting the Kubernetes cluster"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets for the database (at least two AZs)"
  type        = list(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide subnets in at least two availability zones."
  }
}

variable "cluster_security_group_id" {
  description = "Security group of the cluster nodes allowed to reach PostgreSQL"
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the IAM OIDC provider for the cluster issuer (EKS, or AKS registered in IAM)"
  type        = string
}

variable "oidc_issuer_url" {
  description = "Cluster OIDC issuer URL matching oidc_provider_arn"
  type        = string
}

variable "bedrock_model_ids" {
  description = "Bedrock foundation-model IDs the service may invoke (configs/providers.yaml)"
  type        = list(string)
  default     = ["amazon.nova-pro-v1:0"]
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

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.r6g.large"
}

variable "db_allocated_storage_gb" {
  description = "Initial database storage"
  type        = number
  default     = 50
}

variable "tags" {
  description = "Tags applied to every resource"
  type        = map(string)
  default     = { "app.kubernetes.io/part-of" = "model-routing-evaluation" }
}
