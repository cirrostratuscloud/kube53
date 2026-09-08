#!/usr/bin/env bash
# Generate a kubeconfig that points kubectl at the kube53 apiserver.
#
# Usage:
#   ./scripts/gen-kubeconfig.sh [API_ENDPOINT] [TOKEN] > kube53.kubeconfig
#
# If args are omitted, they're read from OpenTofu outputs in ../terraform.
# The apiserver presents a real, ACM-issued cert on api.<cluster-domain>, so we
# do NOT skip TLS verification — kubectl validates it like any cluster.
set -euo pipefail

API_ENDPOINT="${1:-}"
TOKEN="${2:-}"

if [[ -z "${API_ENDPOINT}" || -z "${TOKEN}" ]]; then
  TF_DIR="$(cd "$(dirname "$0")/../terraform" && pwd)"
  if [[ -z "${API_ENDPOINT}" ]]; then
    API_ENDPOINT="$(tofu -chdir="${TF_DIR}" output -raw api_endpoint)"
  fi
  if [[ -z "${TOKEN}" ]]; then
    TOKEN="$(tofu -chdir="${TF_DIR}" output -raw kubeconfig_token)"
  fi
fi

CLUSTER_NAME="kube53"

cat <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER_NAME}
    cluster:
      server: ${API_ENDPOINT}
contexts:
  - name: ${CLUSTER_NAME}
    context:
      cluster: ${CLUSTER_NAME}
      user: ${CLUSTER_NAME}
      namespace: default
current-context: ${CLUSTER_NAME}
users:
  - name: ${CLUSTER_NAME}
    user:
      token: ${TOKEN}
YAML
