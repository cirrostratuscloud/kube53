# The public hosted zone. This zone is the Kubernetes datastore ("etcd").
# Every k8s object lives here as a TXT record.
resource "aws_route53_zone" "kube53" {
  name    = var.cluster_domain
  comment = "kube53 cluster state (Kubernetes objects stored as TXT records)."
}

# ---------------------------------------------------------------------------
# ACM certificate for the cluster domain (and the API endpoint).
# DNS validated against the zone above. You must delegate the zone in the
# parent (create the NS records from the `zone_ns_records` output) BEFORE the
# validation records can resolve publicly.
# ---------------------------------------------------------------------------
resource "aws_acm_certificate" "kube53" {
  domain_name               = var.cluster_domain
  subject_alternative_names = ["*.${var.cluster_domain}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "acm_validation" {
  for_each = {
    for dvo in aws_acm_certificate.kube53.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = aws_route53_zone.kube53.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "kube53" {
  certificate_arn         = aws_acm_certificate.kube53.arn
  validation_record_fqdns = [for record in aws_route53_record.acm_validation : record.fqdn]
}

# ---------------------------------------------------------------------------
# The cluster marker. Its mere existence tells the reconciler to stand up the
# ECS cluster on the next tick (see src/reconciler: EnsureCluster). Storing it
# as a normal TXT record keeps cluster creation declarative — `apply` creates
# it, `destroy` removes it — and matches the datastore wire format used for
# every other object: a base64(json) value under the k53.<domain> label.
# ---------------------------------------------------------------------------
resource "aws_route53_record" "cluster_marker" {
  zone_id = aws_route53_zone.kube53.zone_id
  name    = "_cluster.k53.${var.cluster_domain}"
  type    = "TXT"
  ttl     = 5
  records = [base64encode(jsonencode({ kind = "Cluster", spec = {} }))]
}
