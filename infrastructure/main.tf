provider "aws" {
  region = var.region

  default_tags {
    tags = merge({
      Project = "kube53"
    }, var.tags)
  }
}

locals {
  name = var.name
}

# A static token kubectl will present to the apiserver. Generated if not supplied.
resource "random_password" "api_token" {
  count   = var.api_token == "" ? 1 : 0
  length  = 40
  special = false
}

locals {
  api_token = var.api_token != "" ? var.api_token : random_password.api_token[0].result
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

module "kube53" {
  source = "./modules/kube53"

  name           = local.name
  region         = var.region
  cluster_domain = var.cluster_domain

  hosted_zone_id  = aws_route53_zone.kube53.zone_id
  acm_certificate = aws_acm_certificate_validation.kube53.certificate_arn

  vpc_id             = module.vpc.vpc_id
  public_subnet_ids  = module.vpc.public_subnets
  private_subnet_ids = module.vpc.private_subnets

  api_token                  = local.api_token
  reconcile_interval_minutes = var.reconcile_interval_minutes
}
