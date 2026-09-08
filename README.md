# kube53 🤠☁️

**Kubernetes. On Route53.**

Route53 is already the best database AWS offers, as well as a very fine filesystem. Starting today, it runs Kubernetes as well. Because why not.

---

## Overview

- **Route53 is etcd.** Every Kubernetes object you `kubectl apply` is serialized and
  stored as **TXT records** in a public hosted zone. The DNS zone *is* the cluster state.
- **A Lambda is the API server.** API Gateway + Lambda speak just enough of the
  Kubernetes REST API that real `kubectl` can `create`, `get`, and `delete` objects.
  Writes land in Route53 as TXT records.
- **EventBridge + Step Functions + Lambda are the controller manager.** On a tick, a
  Step Function reads desired state out of Route53 and reconciles real AWS resources.
- **ECS/Fargate + ALB are the kubelet + kube-proxy.** Pods are tasks, Services get an
  ECS service behind a load balancer, Ingress gets an ALB, CronJobs get EventBridge
  Scheduler + `RunTask`.

```
kubectl ──HTTPS──▶ API Gateway ──▶ apiserver Lambda ──▶ Route53 TXT records (etcd)
                                                              │
                       EventBridge tick ──▶ Step Function ──▶ reconciler Lambda
                                                              │
                                          ┌───────────────────┼────────────────────┐
                                          ▼                   ▼                     ▼
                                     ECS cluster         ALB + TG            EventBridge
                                     + services        (Ingress/Svc)      Scheduler (CronJob)
```

## Supported "Kubernetes"

| Kubernetes kind | kube53 realization |
|---|---|
| `Namespace`     | a label prefix on TXT record names |
| `Pod` (`v1`, read-only) | synthesized from live ECS tasks so `kubectl get pods` works |
| `Deployment` (`apps/v1`) | the workload spec (image, replicas, port) for a Service |
| `Service` (`v1`) | ECS service (from the matching Deployment) + ALB target group + listener |
| `Ingress` (`networking.k8s.io/v1`) | ALB + host/path listener rules |
| `CronJob` (`batch/v1`) | EventBridge Scheduler schedule → ECS `RunTask` |
| the "cluster" itself | the ECS cluster (created by the reconciler, **not** OpenTofu) |

## Repo layout

```
.
├── infrastructure/
│   ├── main.tf              # root: wires VPC + zone + ACM + kube53 module
│   ├── variables.tf         # region (default eu-west-1), domain, cidr, etc.
│   ├── outputs.tf           # NS records to delegate, api endpoint, kubeconfig hint
│   ├── versions.tf
│   ├── vpc.tf               # 3-tier VPC, single NAT GW
│   ├── dns.tf               # public hosted zone + ACM (DNS validated)
│   └── modules/
│       └── kube53/          # control plane + reconciler
├── src/                     # Lambda source (apiserver + reconciler)
│   ├── apiserver/
│   └── reconciler/
├── examples/                # sample kubectl manifests
└── scripts/                 # kubeconfig generation, helpers
```

## Order of operations

1. `tofu -chdir=infrastructure apply -target=aws_route53_zone.kube53`
   — create the zone first so you can delegate it.
2. Delegate: create the `NS` records in the **parent** `example.com` zone using
   the `zone_ns_records` output.
3. `tofu -chdir=infrastructure apply` — everything else, including ACM
   (DNS validation now resolves through the delegated zone).
4. `./scripts/gen-kubeconfig.sh > kube53.kubeconfig`
5. `kubectl --kubeconfig kube53.kubeconfig apply -f examples/service.yaml`

> ⚠️ This provisions real, billable AWS resources (ALB, Fargate, NAT GW). Tear it down
> with `tofu destroy` when you're finished.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how the system works.
