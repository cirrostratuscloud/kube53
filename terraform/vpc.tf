data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # 3 tiers, evenly carved out of the VPC CIDR:
  #   public       -> ALBs, NAT GW           (10.53.0.0/20, .16.0/20, ...)
  #   private (app) -> ECS/Fargate tasks      (10.53.64.0/20, ...)
  #   data (intra)  -> reserved for stateful   (10.53.128.0/20, ...)  (no egress)
  public_subnets  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  private_subnets = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i + 4)]
  data_subnets    = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i + 8)]
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.2"

  name = "${var.name}-vpc"
  cidr = var.vpc_cidr
  azs  = local.azs

  public_subnets  = local.public_subnets
  private_subnets = local.private_subnets
  intra_subnets   = local.data_subnets # "data" tier: no NAT, no egress

  # Single NAT gateway, as requested (cheap; not HA).
  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags = {
    Tier                     = "public"
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    Tier                              = "private-app"
    "kubernetes.io/role/internal-elb" = "1"
  }
  intra_subnet_tags = {
    Tier = "private-data"
  }
}
