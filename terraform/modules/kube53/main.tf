terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.63"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # The label under which all datastore records live: <thing>.k53.<domain>
  data_suffix = "k53.${var.cluster_domain}"

  # Common env for both Lambdas so they agree on the schema.
  common_env = {
    HOSTED_ZONE_ID = var.hosted_zone_id
    CLUSTER_DOMAIN = var.cluster_domain
    DATA_SUFFIX    = local.data_suffix
    CLUSTER_NAME   = var.name
    REGION         = var.region
  }

  tags = {
    "k53.io/component" = "control-plane"
  }
}
