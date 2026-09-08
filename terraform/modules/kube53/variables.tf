variable "name" {
  description = "Name prefix."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "cluster_domain" {
  description = "Cluster domain / hosted zone name."
  type        = string
}

variable "hosted_zone_id" {
  description = "Route53 hosted zone id that acts as the datastore."
  type        = string
}

variable "acm_certificate" {
  description = "Validated ACM certificate ARN for the API custom domain."
  type        = string
}

variable "vpc_id" {
  description = "VPC id."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet ids (for ALBs)."
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Private subnet ids (for Fargate tasks)."
  type        = list(string)
}

variable "api_token" {
  description = "Static bearer token kubectl presents."
  type        = string
  sensitive   = true
}

variable "reconcile_interval_minutes" {
  description = "Reconcile loop period in minutes."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  description = "CloudWatch log retention for Lambdas/Step Functions."
  type        = number
  default     = 14
}
