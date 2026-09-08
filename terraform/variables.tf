variable "region" {
  description = "AWS region to deploy kube53 into. Configurable; defaults to eu-west-1."
  type        = string
  default     = "eu-west-1"
}

variable "name" {
  description = "Name prefix for all resources."
  type        = string
  default     = "kube53"
}

variable "cluster_domain" {
  description = "Public hosted zone / cluster domain. This zone becomes the Kubernetes datastore."
  type        = string
  default     = "kube53.example.com"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.53.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to spread the 3 tiers across."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3 (ALBs want >=2 AZs)."
  }
}

variable "api_token" {
  description = <<-EOT
    Static bearer token that kubectl presents to the kube53 apiserver.
    If left empty, a random token is generated and exposed via the
    `kubeconfig_token` output. Treat it like a kubeconfig credential.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "reconcile_interval_minutes" {
  description = "How often the reconciler tick fires (the control loop period)."
  type        = number
  default     = 1
}

variable "tags" {
  description = "Extra tags applied to all resources."
  type        = map(string)
  default     = {}
}
