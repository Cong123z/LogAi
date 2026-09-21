# LogAI Engine on Kubernetes (K8s)

This directory contains Kubernetes manifests for deploying the LogAI Engine stack.

Just like Docker Compose uses `.env`, Kubernetes here uses **`k8s/.env`**! You only have to edit **one single file**.

---

## Architecture

The deployment runs **both the Realtime Engine and the Template Explorer Web UI inside a single Pod** sharing a Persistent Volume:

```
┌───────────────────────────────────────────────────────────────┐
│ Kubernetes Pod: logai                                         │
│                                                               │
│  ┌───────────────────────┐         ┌───────────────────────┐  │
│  │ Container: engine     │         │ Container: web        │  │
│  │ - scripts/run_realtime│         │ - scripts/run_web.py  │  │
│  │ - Port 9108 (metrics) │         │ - Port 5555 (UI)      │  │
│  └──────────┬────────────┘         └───────────▲───────────┘  │
│             │ (Read-Write)                     │ (Read-Only)  │
│             ▼                                  │              │
│       ┌────────────────────────────────────────┴──────┐       │
│       │ Volume: logai-data-pvc (/app/data)            │       │
│       │ - template_registry.json                      │       │
│       │ - group_registry.json                         │       │
│       │ - models/                                     │       │
│       └───────────────────────────────────────────────┘       │
└───────────────────────────────────────────────────────────────┘
```

### Why this guarantees data consistency:
- Both containers run in the **same Pod**, sharing the exact same `logai-data-pvc` mounted at `/app/data`.
- When the engine discovers and flushes new templates to `template_registry.json`, the web UI immediately serves them from that exact same file.
- The web container mounts `/app/data` as `readOnly: true`, preventing accidental state corruption.
- Works with standard Kubernetes `ReadWriteOnce` storage (AWS EBS, GKE Persistent Disk, Azure Disk, local-path). No complex NFS required!

---

## Quick Start Guide (Identical to Docker Compose!)

### Step 1: Create and edit your `.env` file

```bash
# 1. Copy the example file
cp k8s/.env.example k8s/.env

# 2. Edit with your settings
nano k8s/.env
```

**Inside `k8s/.env`:**
```ini
# Container image (change to your registry path if using a private registry)
LOGAI_IMAGE=logai-engine:latest

# Elasticsearch URL (in-cluster or external)
LOGAI_ES_HOSTS=http://elasticsearch.default.svc.cluster.local:9200
LOGAI_ES_USER=elastic
LOGAI_ES_PASSWORD=your-password

# Prometheus metrics port
LOGAI_METRICS_PORT=9108

# Web UI port
LOGAI_WEB_PORT=5555

# Storage paths & sizes
LOGAI_STORAGE_BASE_DIR=/app/data
HF_HOME=/app/hf-cache
LOGAI_DATA_STORAGE_SIZE=10Gi
LOGAI_HF_STORAGE_SIZE=5Gi
```

---

### Step 2: (Optional) Run Initial Training Job

If you have historical logs in Elasticsearch and want to pre-train template clusters and anomaly models before starting realtime processing:

```bash
kubectl apply -f k8s/training-job.yaml
```

Check logs of the training job:
```bash
kubectl logs -f job/logai-training
```

Once completed, the trained models and template registries will be saved directly into `logai-data-pvc`.

---

### Step 3: Deploy Realtime Pipeline & Web UI

Deploy everything using Kustomize (native to `kubectl`):

```bash
kubectl apply -k k8s/
```

> **How it works:** `kubectl apply -k` automatically reads `k8s/.env`, creates a secure Kubernetes Secret `logai-env`, and injects it into the Pods!

Verify the Pod is running:
```bash
kubectl get pods -l app.kubernetes.io/name=logai
```
You should see `2/2` containers running (`logai-engine` + `logai-web`).

---

### Step 4: Access the Web UI & Metrics

Forward the ports to your local machine:

```bash
# Forward Web UI (port 5555)
kubectl port-forward svc/logai-web 5555:5555

# Forward Metrics (port 9108)
kubectl port-forward svc/logai-metrics 9108:9108
```

- Open the **Template Explorer**: [http://localhost:5555](http://localhost:5555)
- View **Prometheus Metrics**: [http://localhost:9108/metrics](http://localhost:9108/metrics)

---

## Updating Environment Variables

Whenever you edit `k8s/.env`, simply re-run:
```bash
kubectl apply -k k8s/
```
Kubernetes will automatically generate a new Secret version and trigger a rolling update of the Pods with zero manual restarts required!
