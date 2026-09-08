# kube53 end-to-end usage

> ⚠️ This creates real, billable AWS resources: NAT gateway, ALB, Fargate tasks,
> Lambdas, Step Functions.

## 0. Prereqs

- OpenTofu >= 1.6, AWS creds for the target account, `kubectl`, `aws` CLI, `jq`.
- A parent hosted zone for `example.com` you can add NS records to.

## 1. Create the zone first (so you can delegate it)

```bash
tofu -chdir=terraform init
tofu -chdir=terraform apply -target=aws_route53_zone.kube53
```

Grab the nameservers to delegate:

```bash
tofu -chdir=terraform output zone_ns_records
```

## 2. Delegate in the parent zone (you do this by hand)

Create an `NS` record for `kube53.example.com` in the parent
`example.com` zone, using the four nameservers from step 1. Wait for it to
propagate (usually a minute or two):

```bash
dig +short NS kube53.example.com
```

## 3. Apply everything else (VPC, ACM, control plane, reconciler)

ACM DNS validation only resolves once delegation is live, so do this after step 2.

```bash
tofu -chdir=terraform apply
```

Outputs of interest:

```bash
tofu -chdir=terraform output api_endpoint        # https://api.kube53...
tofu -chdir=terraform output -raw kubeconfig_token
```

## 4. Wait for the "cluster" to come up

The ECS cluster itself is deliberately NOT created by OpenTofu. Instead, the
`tofu apply` in step 3 writes a `_cluster` marker record into the datastore
(`aws_route53_record.cluster_marker`), and the reconciler stands up the ECS cluster
when it sees that marker on its next tick.

Within one reconcile tick (default 1 min) the ECS cluster `kube53` appears. Watch it:

```bash
aws ecs describe-clusters --clusters kube53 \
  --query 'clusters[0].status' --output text
```

## 5. Point kubectl at kube53

```bash
./scripts/gen-kubeconfig.sh > kube53.kubeconfig
export KUBECONFIG="$PWD/kube53.kubeconfig"

kubectl version
kubectl api-resources
```

`kubectl api-resources` should list `deployments`, `services`, `ingresses`, and
`cronjobs` — served straight out of Route53.

## 6. Deploy a workload (the payoff)

`examples/service.yaml` is a stock Kubernetes manifest — an `apps/v1` Deployment and a
`v1` Service that selects it by label. No kube53-specific fields.

```bash
kubectl apply -f examples/service.yaml
kubectl get deployments
kubectl get services
kubectl get service web -o yaml     # note status.loadBalancer once reconciled
```

The reconciler matches the Service's `selector` to the Deployment's pod labels and
reads the image/replicas/port from the Deployment — the same way real Kubernetes does.

Under the hood this wrote a TXT record. Prove it:

```bash
dig +short TXT web.default.services.k53.kube53.example.com
```

Within a tick the reconciler builds: a target group, an ECS service (2 Fargate tasks
of nginx) in the private subnets, and a host-based rule on the shared ALB. The Service
becomes reachable:

```bash
# once the ECS tasks are healthy in the target group:
curl -s https://web-default.kube53.example.com | head
```

### Ingress

```bash
kubectl apply -f examples/ingress.yaml
curl -s https://site.kube53.example.com | head
```

### CronJob

```bash
kubectl apply -f examples/cronjob.yaml
aws scheduler list-schedules --query 'Schedules[].Name'
# logs land in /kube53/jobs/default/hello
```

## 7. Inspect your workloads from the CLI

Everything below is served by the kube53 apiserver out of Route53 (desired state)
and live ECS (runtime state), so plain `kubectl` works.

### Objects (desired state, stored as TXT records)

```bash
kubectl get deployments
kubectl get services
kubectl get ingress
kubectl get cronjobs

# all supported kinds at once
kubectl get deploy,svc,ing,cronjobs

# full object incl. the status the reconciler writes back
kubectl get service web -o yaml
kubectl describe service web
kubectl describe ingress site
```

`kubectl get service web -o yaml` shows the address the reconciler published under
`status.loadBalancer.ingress` once the Service has been reconciled.

### Pods (runtime state, synthesized from live ECS tasks)

kube53 has no real Pods — workloads run as Fargate tasks — but the apiserver
synthesizes read-only Pod objects from the running ECS tasks, so this works:

```bash
kubectl get pods                       # one "pod" per running task
kubectl get pods -o wide               # includes pod IP + node (fargate/<az>)
kubectl get pods -n default
kubectl get pod <pod-name> -o yaml     # image, phase, container states, task ARN
```

A Pod's `STATUS` reflects the real ECS task state (`Running`, `Pending`, `Failed`),
and its name is `<service>-<task-id>` (mirroring `<deploy>-<rs>-<pod>` in real k8s).
Pods are read-only here — `kubectl delete pod` / `kubectl run` return `405`
(MethodNotAllowed), because Pods are managed by the reconciler via ECS.

> Note: `kubectl logs` and `kubectl exec` are NOT implemented. Container logs live
> in CloudWatch at `/kube53/tasks/<ns>/<name>`:
> ```bash
> aws logs tail /kube53/tasks/default/web --follow
> ```

### Watch changes live

```bash
kubectl get pods -w        # re-lists on the apiserver's polling interval
kubectl get svc -w
```

### Cross-check against the AWS side (the truth underneath)

```bash
# the ECS service + its running task count
aws ecs describe-services --cluster kube53 \
  --services "$(aws ecs list-services --cluster kube53 --query 'serviceArns[0]' --output text | xargs -n1 basename)" \
  --query 'services[0].{running:runningCount,desired:desiredCount,status:status}'

# target group health (are the tasks actually serving?)
TG=$(aws elbv2 describe-target-groups --query "TargetGroups[?starts_with(TargetGroupName, 'k53-svc')].TargetGroupArn | [0]" --output text)
aws elbv2 describe-target-health --target-group-arn "$TG" \
  --query 'TargetHealthDescriptions[].TargetHealth.State'

# prove the object really lives in Route53
dig +short TXT web.default.services.k53.kube53.example.com
```

## 8. Delete = real teardown

Deleting the object removes its TXT record; the next GC tick reaps the AWS resources.

```bash
kubectl delete -f examples/service.yaml
# next tick: ECS service scaled to 0, deleted; orphan target group removed.
```

## 9. Force a reconcile (don't want to wait for the tick)

```bash
SM=$(tofu -chdir=terraform output -raw reconcile_state_machine_arn 2>/dev/null \
  || aws stepfunctions list-state-machines \
     --query "stateMachines[?name=='kube53-reconcile'].stateMachineArn" --output text)
aws stepfunctions start-execution --state-machine-arn "$SM"
```

## 10. Tear it ALL down

```bash
kubectl delete -f examples/ --ignore-not-found
# wait a tick for GC to remove ECS services / schedules / target groups, then:
tofu -chdir=terraform destroy
```

> If `destroy` complains about the ALB or ECS cluster, it's because the reconciler
> created them (they're not in OpenTofu state). Let GC remove the per-object
> resources, then delete the cluster + shared ALB:
> ```bash
> aws ecs delete-cluster --cluster kube53
> aws elbv2 delete-load-balancer --load-balancer-arn \
>   "$(aws elbv2 describe-load-balancers --names k53-shared --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
> ```

## Troubleshooting

- **`kubectl` hangs on discovery**: the custom domain `api.<domain>` may not have
  propagated, or ACM wasn't validated (check delegation in step 2).
- **Service never gets an address**: check the reconciler logs
  (`/aws/lambda/kube53-reconciler`) and that the `_cluster` marker exists (step 4).
- **Tasks crash-loop**: check `/kube53/tasks/<ns>/<name>` logs; usually a bad image
  or the container doesn't listen on `targetPort`.
- **`kubectl get pods` shows nothing**: the ECS cluster or service may not be up yet
  (Pods are synthesized from running tasks). Confirm with
  `aws ecs list-tasks --cluster kube53`. An empty list is expected before the first
  task reaches `RUNNING`, or after a delete.
