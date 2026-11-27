# kube53 lightning talk: deploy the slide deck on kube53

One `tofu apply` deploys kube53 (from `../../infrastructure`), builds the slide deck
(`../deck`) into a container, pushes it to ECR, and deploys it **onto kube53 with `kubectl`**.
The deck then runs as an ECS service behind an ALB at `https://deck.<your domain>`.

> This is a joke project. It creates real, billable AWS resources (ALB, Fargate, NAT gateway).
> Destroy it when you are done.

## Requirements

- An AWS account and credentials in your shell (`aws sts get-caller-identity` works)
- A domain you control in DNS. kube53 needs a subdomain of it (for example `kube53.example.com`)
- `tofu` (OpenTofu) or `terraform` >= 1.5
- `docker` with `buildx`, and a running Docker daemon (Docker Desktop, Colima, ...)
- `kubectl`, `aws` CLI

## Configure

```
cd talks/infra
cp example.tfvars my.auto.tfvars   # gitignored
```

| Variable | Required | Meaning |
|---|---|---|
| `cluster_domain` | yes | Subdomain for the cluster, e.g. `kube53.example.com`. The deck ends up at `deck.<cluster_domain>`. |
| `parent_zone_name` | no | Route53 zone **in the same AWS account** that contains `cluster_domain` (e.g. `example.com`). If set, DNS delegation is automatic. |
| `region` | no | Defaults to `eu-west-1`. |

## Deploy

### Option A: parent zone is in the same AWS account

Set `parent_zone_name` and run:

```
tofu init
tofu apply
```

### Option B: DNS is somewhere else (or another account)

Leave `parent_zone_name` empty. kube53 needs the delegation before its ACM certificate can validate, so do it in two steps:

```
tofu init
tofu apply -target=module.kube53.aws_route53_zone.kube53
tofu output zone_ns_records
```

Create an `NS` record for `cluster_domain` at your DNS provider with those four name servers, wait until
`dig NS <cluster_domain>` returns them, then:

```
tofu apply
```

## What to expect

- The apply takes roughly 10 to 15 minutes. The last step retries `kubectl apply` for up to 15 minutes while the API domain and certificate come up.
- After the apply, the reconciler runs every minute and creates the ECS service and ALB. Give it a few minutes, then open the `deck_url` output.
- Poke at the cluster:

```
export KUBECONFIG=$PWD/.generated/kube53.kubeconfig
kubectl get deploy,svc,ingress,pods
kubectl get pod -o yaml
```

- Edit the deck in `../deck/index.html` and run `tofu apply` again. The image tag follows the deck's content hash, so changes redeploy.
- The slides contain the author's own links and QR codes. Change them in `../deck/index.html` (QR codes are in `../deck/images/qr*.svg`, made with `qrencode`).

## Destroy

Delete the workload first, so the reconciler removes the ECS service and ALB it created, then destroy:

```
KUBECONFIG=$PWD/.generated/kube53.kubeconfig kubectl delete -f .generated/deck.yaml
# wait a couple of minutes for the reconciler tick
tofu destroy
```

## Troubleshooting

- **`explicit deny` or 403 errors:** check your credentials.
- **`api.<domain>: no such host`:** delegation is not in place yet, or your resolver cached the miss (negative TTL up to 15 minutes).
- **`unknown flag: --platform`:** install `docker-buildx`.
- **Cannot connect to the Docker daemon:** start Docker or Colima.
- **Deck not updating:** the reconciler needs a tick (about a minute) after the new image tag is applied.
