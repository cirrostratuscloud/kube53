terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws   = { source = "hashicorp/aws", version = "~> 6.63" }
    null  = { source = "hashicorp/null", version = "~> 3.2" }
    local = { source = "hashicorp/local", version = "~> 2.5" }
  }
}

variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "cluster_domain" {
  description = "Domain for the kube53 cluster, e.g. kube53.example.com. You must control its parent domain's DNS."
  type        = string
}

variable "parent_zone_name" {
  description = "Optional. Route53 zone in THIS account that contains cluster_domain (e.g. example.com). If set, NS delegation is automatic. If empty, delegate manually (see README)."
  type        = string
  default     = ""
}

provider "aws" {
  region = var.region
}

# ---- kube53 itself (the existing ../../infrastructure root, used as a module) ----
module "kube53" {
  source         = "../../infrastructure"
  region         = var.region
  cluster_domain = var.cluster_domain
}

# Delegate the cluster zone from the parent zone (only when parent_zone_name is set).
data "aws_route53_zone" "parent" {
  count = var.parent_zone_name != "" ? 1 : 0
  name  = var.parent_zone_name
}

resource "aws_route53_record" "delegation" {
  count           = var.parent_zone_name != "" ? 1 : 0
  zone_id         = data.aws_route53_zone.parent[0].zone_id
  name            = var.cluster_domain
  type            = "NS"
  ttl             = 300
  records         = module.kube53.zone_ns_records
  allow_overwrite = true
}

moved {
  from = aws_route53_record.delegation
  to   = aws_route53_record.delegation[0]
}

# ---- ECR + image build ----
resource "aws_ecr_repository" "deck" {
  name         = "kube53-deck"
  force_delete = true
}

locals {
  registry = split("/", aws_ecr_repository.deck.repository_url)[0]
  deck_dir = "${path.module}/../deck"
  deck_hash = sha1(join("", [
    for f in sort(fileset(local.deck_dir, "**")) : filesha1("${local.deck_dir}/${f}")
  ]))
  # Tag by content hash so the manifest changes whenever the deck does.
  image = "${aws_ecr_repository.deck.repository_url}:${substr(local.deck_hash, 0, 12)}"
}

resource "null_resource" "build" {
  triggers = { hash = local.deck_hash, repo = aws_ecr_repository.deck.repository_url }
  provisioner "local-exec" {
    command = <<-EOT
      set -e
      aws ecr get-login-password --region ${var.region} | docker login --username AWS --password-stdin ${local.registry}
      docker buildx build --platform linux/amd64 -t ${local.image} --push ${local.deck_dir}
    EOT
  }
}

# ---- deploy the deck onto kube53 with kubectl ----
resource "local_file" "manifest" {
  filename = "${path.module}/.generated/deck.yaml"
  content  = templatefile("${path.module}/deck.yaml.tpl", { image = local.image, host = "deck.${var.cluster_domain}" })
}

resource "local_sensitive_file" "kubeconfig" {
  filename = "${path.module}/.generated/kube53.kubeconfig"
  content = yamlencode({
    apiVersion      = "v1"
    kind            = "Config"
    clusters        = [{ name = "kube53", cluster = { server = module.kube53.api_endpoint } }]
    users           = [{ name = "kube53", user = { token = module.kube53.kubeconfig_token } }]
    contexts        = [{ name = "kube53", context = { cluster = "kube53", user = "kube53", namespace = "default" } }]
    current-context = "kube53"
  })
}

resource "null_resource" "deploy" {
  triggers   = { manifest = local_file.manifest.content_sha256 }
  depends_on = [null_resource.build, aws_route53_record.delegation, local_sensitive_file.kubeconfig]
  provisioner "local-exec" {
    command = <<-EOT
      set -e
      export KUBECONFIG=${local_sensitive_file.kubeconfig.filename}
      for i in $(seq 1 90); do
        kubectl apply --validate=false -f ${local_file.manifest.filename} && exit 0
        echo "api not ready yet, retrying ($i/90)"; sleep 10
      done
      exit 1
    EOT
  }
}

output "zone_ns_records" {
  description = "NS records to create for cluster_domain in its parent zone (manual delegation)."
  value       = module.kube53.zone_ns_records
}

output "deck_url" {
  value = "https://deck.${var.cluster_domain}"
}

output "kubeconfig" {
  value = "KUBECONFIG=talks/infra/.generated/kube53.kubeconfig kubectl get pods"
}
