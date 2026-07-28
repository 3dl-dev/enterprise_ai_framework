#!/usr/bin/env bash
# Build an image in the cluster with kaniko and push it to the rail registry.
#
#   deploy/bin/kaniko-build.sh <context-dir> <image-ref> [--build-arg K=V ...]
#
# Why kaniko and not `docker build`: this cluster's real job is GPU training and the
# operator agreement is that we get a namespace, not a Docker socket. kaniko needs
# neither a daemon nor a privileged container — it unpacks layers in its own filesystem.
#
# The build context is carried in as a gzipped tar inside a ConfigMap (binaryData), which
# keeps this to one kubectl round trip and no shared volume. ConfigMaps cap at ~1MiB, so
# this is for small contexts — the two we have are a Dockerfile plus a handful of small
# files. The script fails loudly rather than truncating if a context outgrows that.
set -euo pipefail

NS=enterprise-ai
REGISTRY="${RAIL_REGISTRY:-192.168.2.43:30500}"

CONTEXT="${1:?usage: kaniko-build.sh <context-dir> <image-ref> [--build-arg K=V ...]}"
IMAGE="${2:?usage: kaniko-build.sh <context-dir> <image-ref> [--build-arg K=V ...]}"
shift 2

[[ -f "${CONTEXT}/Dockerfile" ]] || { echo "no Dockerfile in ${CONTEXT}" >&2; exit 1; }

JOB="kaniko-$(echo -n "${IMAGE}" | sha256sum | cut -c1-8)-$(date +%s)"
CM="${JOB}-ctx"
TARBALL="$(mktemp -t kaniko-ctx-XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT

tar -czf "$TARBALL" -C "$CONTEXT" .
SIZE=$(stat -c %s "$TARBALL")
if (( SIZE > 700000 )); then
    echo "build context is ${SIZE} bytes; ConfigMaps cap near 1MiB. Use a git or https" >&2
    echo "context instead of this script for a context this large." >&2
    exit 1
fi
echo "==> context ${CONTEXT} -> ${SIZE} bytes"

kubectl -n "$NS" create configmap "$CM" --from-file=context.tar.gz="$TARBALL" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

BUILD_ARGS=""
for a in "$@"; do
    BUILD_ARGS="${BUILD_ARGS}
            - \"${a}\""
done

# --insecure / --skip-tls-verify: the rail registry is plain HTTP on the LAN. That is the
# cluster's existing arrangement (the control-plane image is already pulled from it), not
# something introduced here.
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
  namespace: ${NS}
  labels:
    app.kubernetes.io/part-of: enterprise-ai-framework
    app.kubernetes.io/component: image-build
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels: { app: kaniko-build }
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      initContainers:
        - name: unpack
          image: busybox:1.36
          command: ["sh", "-c", "tar -xzf /ctx-src/context.tar.gz -C /ctx && ls -la /ctx"]
          volumeMounts:
            - { name: ctx-src, mountPath: /ctx-src }
            - { name: ctx, mountPath: /ctx }
          resources:
            requests: { cpu: "50m", memory: "64Mi" }
            limits:   { cpu: "500m", memory: "256Mi" }
      containers:
        - name: kaniko
          image: gcr.io/kaniko-project/executor:v1.23.2
          args:
            - "--context=dir:///ctx"
            - "--dockerfile=/ctx/Dockerfile"
            - "--destination=${IMAGE}"
            - "--insecure"
            - "--skip-tls-verify"
            - "--single-snapshot"${BUILD_ARGS}
          volumeMounts:
            - { name: ctx, mountPath: /ctx }
          resources:
            requests: { cpu: "500m", memory: "2Gi", ephemeral-storage: "4Gi" }
            limits:   { cpu: "3",    memory: "6Gi", ephemeral-storage: "20Gi" }
      volumes:
        - name: ctx-src
          configMap: { name: ${CM} }
        - name: ctx
          emptyDir: {}
YAML

echo "==> build job ${JOB}"
kubectl -n "$NS" wait --for=condition=Ready pod -l job-name="${JOB}" --timeout=180s 2>/dev/null || true
kubectl -n "$NS" logs -f "job/${JOB}" --all-containers=true --tail=-1 2>/dev/null || true

# Polled rather than `kubectl wait`: wait takes one condition, and waiting on `complete`
# alone hangs for the full timeout when the build fails — which is the case you most want
# to hear about quickly.
STATUS=""
for _ in $(seq 1 360); do
    STATUS=$(kubectl -n "$NS" get "job/${JOB}" \
        -o jsonpath='{.status.conditions[?(@.status=="True")].type}' 2>/dev/null || true)
    [[ -n "$STATUS" ]] && break
    sleep 5
done
kubectl -n "$NS" delete configmap "$CM" >/dev/null 2>&1 || true
if [[ "$STATUS" != *Complete* ]]; then
    echo "build FAILED (job status: ${STATUS:-running})" >&2
    kubectl -n "$NS" logs "job/${JOB}" --all-containers=true --tail=80 >&2 || true
    exit 1
fi
echo "==> pushed ${IMAGE}"
