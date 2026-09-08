# kube53 architecture: how the system works

## 1. Route53 as etcd — the datastore schema

Every Kubernetes object is stored as one **TXT record** in the cluster hosted zone.
DNS is the source of truth for *desired state*. There is no other database.

### Record naming

```
<name>.<namespace>.<plural-kind>.k53.<cluster-domain>   TXT   "<chunked base64 JSON>"
```

Examples (cluster domain = `kube53.example.com`):

| Object | TXT record name |
|---|---|
| Deployment `web` in `default` | `web.default.deployments.k53.kube53.example.com` |
| Service `web` in `default` | `web.default.services.k53.kube53.example.com` |
| Ingress `site` in `default` | `site.default.ingresses.k53.kube53.example.com` |
| CronJob `report` in `ops` | `report.ops.cronjobs.k53.kube53.example.com` |
| the cluster marker | `_cluster.k53.kube53.example.com` |

The `k53.` label namespaces our data so it never collides with real DNS records
(ACM validation, ALB aliases, etc.) that also live in this zone.

### Record value: chunked base64

A TXT record's string is limited to 255 characters per chunk, and a record set has a
practical size ceiling. We serialize the object to JSON, base64 it, and split into
255-char chunks stored as the multi-string TXT value:

```
"a2luZDogU2Vydmlj..." "ZQ1hcGlWZXJzaW9u..." ...
```

Readers concatenate all chunks, base64-decode, and `json.loads`. If an object is too
large for a single record set, the apiserver rejects the write with `413`.

### Metadata we synthesize

- `metadata.uid` — deterministic UUIDv5 of `kind/namespace/name`.
- `metadata.resourceVersion` — the Route53 change id isn't stable, so we keep a
  monotonic counter in the object's own annotation `k53.io/rv` bumped on each write.
- `metadata.creationTimestamp` — set on first write, preserved after.

## 2. The apiserver Lambda (control plane)

API Gateway (HTTP API, custom domain `api.<cluster-domain>` w/ the ACM cert) →
one Lambda. It implements just enough of the Kubernetes REST surface for kubectl:

| Route | Purpose |
|---|---|
| `GET /api` | core API versions (so discovery works) |
| `GET /apis` | API group list (`networking.k8s.io`, `batch`) |
| `GET /apis/{group}/{version}` | resources in a group |
| `GET /api/v1` | core v1 resources (services, namespaces) |
| `GET /version` | fake version so `kubectl version` is happy |
| `GET /openapi/v2`,`/openapi/v3*` | minimal/empty schema docs |
| `GET .../{plural}` | list — scans TXT records for that kind |
| `GET .../{plural}/{name}` | get one |
| `POST .../{plural}` | create — writes a TXT record |
| `PUT .../{plural}/{name}` | replace |
| `PATCH .../{plural}/{name}` | strategic/merge/apply patch (best-effort merge) |
| `DELETE .../{plural}/{name}` | delete the TXT record |

Auth: static bearer token (from Terraform) checked against the `Authorization` header.
This is the token baked into the generated kubeconfig.

`kubectl apply` uses client-side apply by default (`kubectl apply` → GET then PATCH or
POST). We support the apply-patch content types well enough that `kubectl apply -f`,
`kubectl get`, `kubectl delete` all work against our supported kinds.

### Synthetic, read-only Pods

`Pod` (core `v1`) is a special case: there are no Pod objects in the datastore.
Instead the apiserver builds Pod objects on the fly from live ECS tasks in the
cluster (`ecs:ListTasks`/`DescribeTasks`, mapped back to their Service via the
`k53.io/key` tag), so `kubectl get pods` / `get pod -o yaml` return real runtime
status (phase, container state, pod IP, node). Pods are read-only — writes return
`405`, because the reconciler owns task lifecycle via ECS. This is the one place the
apiserver reads AWS runtime state rather than Route53 desired state.

## 3. The reconciler (controller manager)

```
EventBridge rule (rate = reconcile_interval_minutes)
    └─▶ Step Function (kube53-reconcile)
            ├─ EnsureCluster      (create ECS cluster if _cluster object exists)
            ├─ ListDesiredState   (read all k53 TXT records)
            ├─ ReconcileServices  (ECS service + ALB target group/listener)
            ├─ ReconcileIngresses (ALB + host/path rules)
            ├─ ReconcileCronJobs  (EventBridge Scheduler -> ECS RunTask)
            └─ GarbageCollect     (delete AWS resources with no backing object)
```

The Step Function is mostly orchestration + retry/catch; the actual AWS mutations
happen in the reconciler Lambda invoked per-phase. Reconciliation is **level-based and
idempotent**: it computes desired vs actual every tick and converges. Nothing depends on
having caught a specific event.

### Mapping details

- **cluster** (`_cluster` marker present — created declaratively by OpenTofu's
  `aws_route53_record.cluster_marker`): create ECS cluster `kube53`, a shared
  Fargate capacity provider association, and a security group. Tagged
  `k53.io/managed=true`.
- **Deployment + Service**: the Service is matched to a Deployment whose pod-template
  labels satisfy the Service's `spec.selector` (standard label matching). The
  container image, replica count, and port come from the Deployment's pod spec. We
  register an ECS task definition from that container, create an ECS service, a
  target group, and a host-based rule on the shared cluster ALB. (Back-compat: a
  Service may still carry a `k53.io/image` annotation to define the workload inline
  without a Deployment.) The Service's external address is written back
  into an `A`/alias record `svc-<name>.<ns>.<cluster-domain>` and surfaced in
  `status.loadBalancer.ingress`.
- **Ingress**: an ALB (the shared one) with listener rules per `spec.rules[].host` and
  `http.paths[].path` forwarding to the target group of the referenced Service.
- **CronJob**: an EventBridge **Scheduler** schedule using `spec.schedule` (cron/rate)
  with a target of ECS `RunTask` on the cluster, task def derived from
  `spec.jobTemplate`.

### Why level-based (not event-driven writes)?

Route53 has no change stream we can subscribe to cheaply. Polling the zone on a tick and
diffing is simple, robust, and forgiving of partial failures — the same reason real
Kubernetes controllers are level-triggered. The EventBridge tick is our informer resync.

## 4. Failure & safety posture

- All reconciler-created AWS resources are tagged `k53.io/managed=true` and
  `k53.io/key=<kind>/<ns>/<name>` so GC only ever touches things kube53 created.
- The Step Function uses `Catch`/`Retry` so one bad object can't wedge the loop.
- The apiserver validates supported kinds and rejects everything else with a clean
  `NotFound`/`MethodNotAllowed`, so kubectl gives sane errors instead of hanging.
- Deleting a kube53 object marks it for GC; the next tick tears the AWS resources down.
