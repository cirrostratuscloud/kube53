apiVersion: apps/v1
kind: Deployment
metadata: {name: deck, namespace: default, labels: {app: deck}}
spec:
  replicas: 1
  selector: {matchLabels: {app: deck}}
  template:
    metadata: {labels: {app: deck}}
    spec:
      containers:
        - name: deck
          image: ${image}
          ports: [{containerPort: 80}]
---
apiVersion: v1
kind: Service
metadata: {name: deck, namespace: default}
spec:
  selector: {app: deck}
  ports: [{port: 80, targetPort: 80, protocol: TCP}]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: deck, namespace: default}
spec:
  rules:
    - host: ${host}
      http:
        paths:
          - {path: /, pathType: Prefix, backend: {service: {name: deck, port: {number: 80}}}}
