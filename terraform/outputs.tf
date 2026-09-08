output "zone_ns_records" {
  description = <<-EOT
    Delegate the cluster zone by creating THESE as the NS records for the
    cluster domain in its parent hosted zone.
    Do this after the first targeted apply and before the full apply so ACM
    DNS validation can resolve.
  EOT
  value       = aws_route53_zone.kube53.name_servers
}

output "hosted_zone_id" {
  description = "The Route53 zone that stores all cluster state."
  value       = aws_route53_zone.kube53.zone_id
}

output "acm_certificate_arn" {
  description = "Validated ACM cert for the cluster domain."
  value       = aws_acm_certificate_validation.kube53.certificate_arn
}

output "api_endpoint" {
  description = "The kube53 apiserver endpoint kubectl talks to."
  value       = module.kube53.api_endpoint
}

output "kubeconfig_token" {
  description = "Bearer token for kubectl. Feed into scripts/gen-kubeconfig.sh."
  value       = local.api_token
  sensitive   = true
}

output "gen_kubeconfig_command" {
  description = "Copy/paste to produce a working kubeconfig."
  value       = "./scripts/gen-kubeconfig.sh ${module.kube53.api_endpoint} > kube53.kubeconfig"
}
