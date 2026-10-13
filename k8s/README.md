# LogAI Engine on Kubernetes (K8s)

This directory contains Kubernetes manifests for deploying the LogAI Engine stack.

```
k8s/
├── base/                 shared Deployment, Services, PVC (no local files needed)
├── overlays/
│   ├── local/            manual deploy from one .env file (Quick Start below)
│   └── oracle/           GitOps deploy with Argo CD (see "GitOps with Argo CD")
├── argocd/               the Argo CD Application for overlays/oracle
├── training-job.yaml     optional one-off training Job
└── embedding-config.example.yaml
```

For a manual deploy, Kubernetes uses **`k8s/overlays/local/.env`**, just like Docker Compose uses `.env`. You only have to edit **one single file**.

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
│             │ (Read-Write)                     │ (Intents)    │
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
- The web container writes only intent files (documentation, grouping
  overrides, index selection, retrain schedule, analysis requests, incidents,
  LLM profiles); registries, status and LLM results remain engine-owned.
- Readiness executes `scripts/healthcheck.py`, which requires a fresh engine
  heartbeat instead of treating an open TCP port as pipeline progress.
- Port 5555 exposes unauthenticated mutation endpoints. Restrict it with an
  ingress authentication layer or a trusted-network policy.
- Works with standard Kubernetes `ReadWriteOnce` storage (AWS EBS, GKE Persistent Disk, Azure Disk, local-path). No complex NFS required!

---

## Quick Start Guide (Identical to Docker Compose!)

### Step 1: Create and edit your `.env` file

```bash
# 1. Copy the example file
cp k8s/overlays/local/.env.example k8s/overlays/local/.env

# 2. Edit with your settings
nano k8s/overlays/local/.env
```

Every key (image, Elasticsearch, ports, storage size, embedding API key, LLM)
is listed with comments in `k8s/overlays/local/.env.example`.

`LOGAI_ES_*` are only defaults. The Elasticsearch endpoint, username, password
and TLS check can also be set on the web **Data sources** page; that overrides
the Secret without a redeploy, and **Use server default** goes back to it.

---

### Step 2: Link the BGE-M3 service

Create `logai-embedding` in the same namespace as LogAI. Start from
`k8s/embedding-config.example.yaml`, replace the endpoint with the Service URL
provided by the cluster administrator, and set `LOGAI_EMBEDDING_API_FORMAT` to
`openai` for `/v1/embeddings` or `tei` for `/embed`.

```bash
cp k8s/embedding-config.example.yaml k8s/embedding-config.yaml
# Edit k8s/embedding-config.yaml before applying it.
kubectl apply -f k8s/embedding-config.yaml
```

The realtime Deployment and training Job both require this ConfigMap. Keep an
API key, when required, in `logai-env`; never put it in the ConfigMap.

---

### Step 3: (Optional) Run Initial Training Job

If you have historical logs in Elasticsearch and want to pre-train template clusters and anomaly models before starting realtime processing:

```bash
kubectl apply -f k8s/training-job.yaml
```

Check logs of the training job:
```bash
kubectl logs -f job/logai-training
```

Once completed, the trained models and template registries will be saved directly into `logai-data-pvc`.

**Retraining later:** use the web UI's **Retrain** page (`#retrain`) to schedule retrains or start one now; the engine pauses, trains in its own process and restarts itself inside the running pod (readiness stays green via the retrain heartbeat). Do not run the training Job while the engine runs. Manual fallback: stop the realtime pod first. It writes the registries continuously and only reads them at startup.

```bash
kubectl scale deployment/logai --replicas=0
kubectl delete job logai-training --ignore-not-found
kubectl apply -f k8s/training-job.yaml
kubectl wait --for=condition=complete job/logai-training --timeout=2h
kubectl scale deployment/logai --replicas=1
```

Existing groups never change on a retrain: new templates are only added to them or form new groups (recorded in `group_lineage.json`). A failed or interrupted retrain is rolled back to the previous artifacts. Logs written while the pod is down are read from the checkpoint after restart; watch `time() - logai_last_processed_event_timestamp_seconds` fall back to near zero.

---

### Step 4: Deploy Realtime Pipeline & Web UI

Deploy everything using Kustomize (native to `kubectl`):

```bash
kubectl apply -k k8s/overlays/local
```

> **How it works:** `kubectl apply -k` automatically reads `k8s/overlays/local/.env`, creates a secure Kubernetes Secret `logai-env`, and injects it into the Pods!

Verify the Pod is running:
```bash
kubectl get pods -l app.kubernetes.io/name=logai
```
You should see `2/2` containers running (`logai-engine` + `logai-web`).

---

### Step 5: Access the Web UI & Metrics

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

Whenever you edit `k8s/overlays/local/.env`, simply re-run:
```bash
kubectl apply -k k8s/overlays/local
```
Kubernetes will automatically generate a new Secret version and trigger a rolling update of the Pods with zero manual restarts required!

---

## GitOps with Argo CD (Oracle Cloud cluster)

`k8s/overlays/oracle` is deployed by Argo CD, with Git as the source of truth.
Unlike `base`, it runs the engine and the web UI as two Deployments
(`logai-engine`, `logai-web`) sharing the ReadWriteMany PVC `logai-data-pvc`,
which is how the Oracle cluster was first set up; names and selectors match
those objects so Argo CD adopts them in place.

1. A pull request into `master` runs the CI in `.github/workflows/ci-cd.yml`:
   tests, manifest checks, and an arm64 image build with a Trivy scan.
2. On merge, CI pushes `ghcr.io/cong123z/logai:sha-<commit>` and commits that
   tag into `overlays/oracle/kustomization.yaml`.
3. Argo CD sees the commit and syncs the namespace `logai`. The `Recreate`
   strategy stops the old pod before the new one mounts the PVC, and the engine
   resumes from its checkpoint.
4. To roll back, `git revert` the deploy commit.

What lives where:

| Setting | Where | In Git? |
| --- | --- | --- |
| Image tag | `overlays/oracle/kustomization.yaml` (`images:`) | Yes, written by CI |
| Non-secret config | `overlays/oracle/params.env` → ConfigMap `logai-config` | Yes |
| Credentials (`LOGAI_ES_USER`, `LOGAI_ES_PASSWORD`, `LOGAI_EMBEDDING_API_KEY`, `LOGAI_LLM_API_KEY`) | Secret `logai-env` | **No**, created by hand |
| Embedding endpoint | ConfigMap `logai-embedding` | No, created by hand |

One-time setup, in order:

```bash
# 1. Credentials (only the keys you use; an empty Secret is fine)
kubectl create secret generic logai-env -n logai \
  --from-literal=LOGAI_ES_USER=... --from-literal=LOGAI_ES_PASSWORD=...

# 2. Embedding ConfigMap, if not already in the namespace
kubectl apply -n logai -f k8s/embedding-config.yaml

# 3. After CI has published a first image on master:
kubectl apply -n argocd -f k8s/argocd/logai-application.yaml
```

The Application starts with manual sync. Review the diff in Argo CD, sync once,
then enable `automated` in `k8s/argocd/logai-application.yaml`. The PVC carries
`Prune=false,Delete=false`, so Argo CD never deletes it.

The GHCR package must be pullable by the cluster: make it public (repository
Packages settings) or add an `imagePullSecret`. Prefer the web UI **Retrain**
page over `training-job.yaml` on this cluster; the Job still references the local
`logai-engine:latest` image.
