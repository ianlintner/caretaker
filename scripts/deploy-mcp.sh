#!/usr/bin/env bash
set -e

# Usage: ./scripts/deploy-mcp.sh [namespace]

NAMESPACE=${1:-caretaker}

echo "Deploying Caretaker MCP Backend to AKS..."
echo "Namespace: $NAMESPACE"

# Create namespace if it doesn't exist
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# Apply manifests
kubectl apply -f infra/k8s/caretaker-mcp-deployment.yaml -n "$NAMESPACE"
kubectl apply -f infra/k8s/caretaker-mcp-service.yaml -n "$NAMESPACE"

echo "Deployment submitted."
echo "Wait for pods to be ready:"
echo "kubectl get pods -n $NAMESPACE -l app=caretaker-mcp -w"
