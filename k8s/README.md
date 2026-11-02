# LogAI (logai2) on Kubernetes (K8s)

This directory contains Kubernetes manifests for deploying the LogAI stack using Kustomize and GitOps.

```
k8s/
├── base/                          shared Deployments, StatefulSet, Services, PVC
│   ├── kustomization.yaml
│   ├── postgres.yaml              PostgreSQL 16 with pgvector StatefulSet & Service
│   ├── engine.yaml                logai-engine Deployment & metrics Service (:9108)
│   ├── web.yaml                   logai-web Deployment & UI Service (:5555)
│   └── linker.yaml                logai-linker Deployment (BGE-M3 embeddings)
├── overlays/
│   ├── oracle/                    GitOps deploy on Oracle Cloud with Argo CD
│   │   ├── kustomization.yaml
│   │   ├── params.env
│   │   ├── pv.yaml                hostPath /u01/logai-pg PV
│   │   └── patch-oracle.yaml      nodeSelector & PVC protection
│   └── local/                     manual deploy using local .env file
│       ├── kustomization.yaml
│       └── .env.example
├── argocd/                        Argo CD Application manifest
│   └── logai-application.yaml
└── embedding-config.example.yaml  BGE-M3 embedding ConfigMap example
```

---

## 1. Quick Start: Local Deployment

```bash
# 1. Copy the example environment file
cp k8s/overlays/local/.env.example k8s/overlays/local/.env

# 2. Edit credentials and ES connection
nano k8s/overlays/local/.env

# 3. Deploy everything using Kustomize
kubectl apply -k k8s/overlays/local
```

---

## 2. GitOps with Argo CD (Oracle Cloud)

`k8s/overlays/oracle` is continuously deployed by Argo CD.

1. **One-Time Setup on the cluster:**
   ```bash
   # Create namespace
   kubectl create namespace logai --dry-run=client -o yaml | kubectl apply -f -

   # Create Secret logai-env (never committed to Git)
   kubectl create secret generic logai-env -n logai \
     --from-literal=POSTGRES_PASSWORD="YourDbPasswordHere123" \
     --from-literal=LOGAI_ES_USER="elastic" \
     --from-literal=LOGAI_ES_PASSWORD="YourElasticPassword" \
     --from-literal=LOGAI_ES_HOSTS="http://elasticsearch.logging.svc:9200" \
     --from-literal=LOGAI_ES_INDEX="hdfs" \
     --from-literal=LOGAI_ES_VERIFY_CERTS="true"

   # Apply BGE-M3 embedding ConfigMap (if not already applied)
   kubectl apply -n logai -f k8s/embedding-config.example.yaml

   # Apply Argo CD Application
   kubectl apply -n argocd -f k8s/argocd/logai-application.yaml
   ```

2. **Automated CI/CD Workflow:**
   - Any push to `master` triggers `.github/workflows/ci-cd.yml`.
   - CI builds the native ARM64 image, scans with Trivy, pushes to GHCR (`ghcr.io/cong123z/logai:sha-<commit>`), and updates `newTag` in `k8s/overlays/oracle/kustomization.yaml`.
   - Argo CD detects the commit and performs an automated rolling update.
