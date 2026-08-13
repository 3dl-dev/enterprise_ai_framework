# Generic Docker (and KVM qemu) inside a workspace — runtime options and runbook

Tracking item: `enterpriseaiframework-ee4`.

## The requirement

A user inside an opencode workspace should be able to run `docker` generically — build
images, run containers, `docker compose`, and boot a VM under qemu — the way any real dev
box does. The forcing case is OVMX (`~/projects/vms`): its build is
`docker build -f Dockerfile.bootable -o dist .` (BuildKit local-export) and its test boots a
kernel under `qemu-system-x86_64`. A stock workspace pod has no build daemon, no `/dev/kvm`,
and no qemu, so both are DOA.

## The constraint that decides the approach

`deploy/bin/kaniko-build.sh` records the operator agreement in one line:

> the operator agreement is that we get a namespace, not a Docker socket.

That is why images are built with kaniko (in-namespace, no daemon) rather than
`docker build`. **The Sysbox path below requires node-level changes** — installing
binaries, editing the k3s containerd config, and restarting `k3s-agent` — which is exactly
the node-level access that agreement says the platform does not have. So the first question
is not technical, it is a boundary question for the cluster operator:

**Do we actually own the k3s nodes enough to install a container runtime on them?**

- **Yes** → take Option A (Sysbox). It is the correct, generic, unprivileged answer.
- **No** → take Option B (rootless in-image). It delivers generic docker *build* with zero
  node changes, but cannot give KVM-accelerated qemu without one minimal node concession
  (`/dev/kvm` via a device plugin); qemu otherwise falls back to slow TCG emulation.

Two more facts that gate execution regardless of option:

1. **SSH to the nodes is currently denied** (`baron@192.168.2.44` → `Permission denied
   (publickey)`). Option A cannot start until node shell access exists.
2. **k3s-worker is the single worker** and runs the entire live platform (gateway, chat,
   postgres, identity, control-plane) plus live workspaces. Enabling Sysbox restarts
   `k3s-agent`, which **bounces every pod on the node** — a full platform outage window, not
   a training blip. Schedule it.

## Licensing (settled)

Sysbox CE is **Apache-2.0** (repo LICENSE, verified). The historical CE cap of 16
Sysbox-containers-per-host — the one user-count trigger that would have failed the
OSI-approved-default rule — was raised to 4K/node and now matches EE. Sysbox EE is
deprecated (folded toward CE / Docker Desktop Hardened), so no paid runtime tier gates the
capability we need. KVM/qemu support is not edition-gated; it is a `/dev/kvm`-exposure
question, independent of CE/EE. Net: Sysbox CE clears the standing OSI-default constraint.

---

## Option A — Sysbox runtime (node-level, unprivileged, generic)

Verified against the official K3s guide "Sysbox Runtime With K3s"
(docs.k3s.io/blog/2025/09/27/k3s-sysbox, 2025-09-27) for **containerd 2.x**, which is our
stack (`containerd 2.3.2-k3s2`, Ubuntu 24.04, kernel 6.8, k3s v1.36). Note the guide's own
caveat: containerd-2.x support is **not yet in a released Sysbox package** — sysbox-runc
must be built from its `main` branch — and the integration "is still evolving." Treat this
as bleeding-edge; expect friction.

Run everything below as root on **each node that schedules workspaces** — today only
`k3s-worker` (192.168.2.44). `k3s-cp` does not run workspaces and must be left alone.

### A1. Build Sysbox from source (containerd-2.x fix lives on `main`, not in a release)

Needs Docker to build. The node has containerd, not Docker — so build the static binaries
on the `workshop` box (has Docker 29.7.x) and copy them over, or install Docker on the node
temporarily. Static-build path:

```bash
git clone --recursive https://github.com/nestybox/sysbox.git
cd sysbox/sysbox-runc && git checkout main && git pull origin main && cd ..
make IMAGE_BASE_DISTRO=ubuntu IMAGE_BASE_RELEASE=jammy sysbox-static
# produces static sysbox-runc / sysbox-mgr / sysbox-fs under sysbox/
sudo make install          # installs binaries + systemd units + subuid/subgid setup
```

### A2. Start the Sysbox daemons

```bash
sudo systemctl enable --now sysbox           # or: sudo sysbox
systemctl status sysbox-mgr sysbox-fs
```

### A3. Register the runtime handler in k3s containerd (2.x config scheme)

k3s regenerates `config.toml` from a template. Create
`/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl` (merge, do not clobber an
existing tmpl):

```toml
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.sysbox-runc]
  runtime_type = "io.containerd.runc.v2"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.sysbox-runc.options]
  SystemdCgroup = false
  BinaryName = "/usr/bin/sysbox-runc"
```

### A4. Restart k3s-agent — THIS IS THE PLATFORM-BOUNCE WINDOW

```bash
sudo systemctl restart k3s-agent   # regenerates config.toml, restarts containerd + ALL pods
```

Every pod on k3s-worker restarts here. Do it in an announced window. Verify after:

```bash
sudo grep -A3 sysbox-runc /var/lib/rancher/k3s/agent/etc/containerd/config.toml
```

### A5. Create the RuntimeClass

```bash
kubectl apply -f deploy/k8s/62-workspace-sysbox.yaml
```

### A6. Expose /dev/kvm for accelerated qemu (skip → qemu runs slow TCG)

Confirm the node has KVM first: `ls -l /dev/kvm` and `grep -c vmx /proc/cpuinfo` on
k3s-worker (present on `workshop`; unverified on the worker). Then install a KVM device
plugin (e.g. kubevirt `device-plugin-kvm`, Apache-2.0) so pods request the device without
`privileged`, and the workspace pod requests `devices.kubevirt.io/kvm: "1"`.

### A7. Workspace image + pod-spec

**Image — DONE and verified** (`deploy/workspace/Dockerfile`, `entrypoint.sh`). The image
now ships the toolchain (`docker.io` + `docker-buildx` + `qemu-system-x86` + `qemu-utils` +
`gosu`, all Debian trixie main) and a conditional-startup contract, covered by
`tests/test_workspace_docker_runtime.py`. The daemon is **inert** unless the pod opts in:
`entrypoint.sh` starts `dockerd` only when `WS_DOCKER=1` **and** it started as container-root,
then drops to the unprivileged `coder` user via `gosu` before serving the shell (the shell
never runs as root, docker or not). So the same image is safe in the default hardened pod
(WS_DOCKER unset → nothing starts) and functional under Sysbox.

**Pod-spec — the exact delta the Sysbox variant of `deploy/k8s/61-workspace.template.yaml`
must add** (NOT yet applied to the shared template — it must not go live before a node runs
Sysbox, or the pod stays in ContainerCreating; wire it behind a provisioner `--docker` opt-in
or a variant file, and verify in the kaniko-build + reprovision loop):

- `spec.runtimeClassName: sysbox-runc`
- `spec.hostUsers: false` — turns on the user-namespace isolation Sysbox maps root through.
- pod `securityContext`: drop `runAsNonRoot: true`; set `runAsUser: 0` / `runAsGroup: 0`
  (container-root, userns-mapped to an unprivileged host UID under Sysbox — *more* isolated
  than today's shared-userns uid-1000, not less). Keep `seccompProfile: RuntimeDefault`.
- container `securityContext`: the current `capabilities: { drop: ["ALL"] }` +
  `allowPrivilegeEscalation: false` must relax enough for `dockerd` (Sysbox scopes those
  caps to the userns, so this is not host privilege). Start from Sysbox's documented default
  and tighten against a real boot — this is the one line that can only be settled on-node.
- env: `WS_DOCKER: "1"` (and optionally `WS_DOCKER_DATA_ROOT`).
- volumes: back the docker data-root with a **sized** volume. The image defaults data-root to
  `/var/lib/docker`; the project PVC is only 5Gi and an OVMX build is larger, so mount an
  emptyDir (or a dedicated PVC) at `WS_DOCKER_DATA_ROOT` sized for a real image build.
- resources: request `devices.kubevirt.io/kvm: "1"` (from A6) for accelerated qemu.

Only the container-`securityContext` cap set and the `/dev/kvm` passthrough genuinely need a
live Sysbox node to finalize; everything else above is settled by the image contract.

### A8. Prove it (definition of done for ee4)

From inside a real workspace (`kubectl -n enterprise-ai exec` into `ws-<user>`), both must
pass:

```bash
docker build -f Dockerfile.bootable -o dist .            # in a checkout of ~/projects/vms
./distro/boot/run-qemu.sh dist/vmlinuz dist/initramfs-ovmx.cpio.gz   # KVM-accelerated
```

---

## Option B — Rootless Docker/BuildKit baked into the image (namespace-only, no node changes)

Honors "a namespace, not a Docker socket": no runtime install, no containerd edit, no agent
restart. Bake rootless dockerd (or just rootless BuildKit + buildx) into
`deploy/workspace/Dockerfile`; kaniko-build + reprovision as usual (no node access needed).

- **Delivers:** generic `docker build`, including `docker build -o dist` (rootless BuildKit
  supports local export) — enough for the OVMX *build*.
- **Caveats:** needs unprivileged user namespaces enabled on the node kernel (Ubuntu 24.04
  default-on) and a `RuntimeDefault` seccomp profile that permits `clone(CLONE_NEWUSER)`
  (default-on); overlayfs needs fuse-overlayfs in the image.
- **Cannot deliver:** KVM-accelerated qemu. `/dev/kvm` still requires a node-level device
  plugin (A6) or a privileged pod. Without it, `qemu-system-x86_64` runs under TCG software
  emulation — correct, but ~10–20× slower. Acceptable for a boot smoke test, painful for
  real work.

Net: Option B unblocks OVMX's build with zero operator involvement, and gets qemu working in
TCG. Accelerated qemu needs the single node concession in A6 regardless of A-vs-B.

---

## Recommendation

Forks on the operator boundary, which is Baron's call:

- If the k3s nodes are genuinely ours to modify → **Option A (Sysbox)**. It is the correct
  generic, unprivileged answer and the licensing is clean. Needs: node ssh access, a
  platform-bounce window, and ratification of the workspace posture change (root-inside +
  relaxed caps under userns).
- If "a namespace, not a Docker socket" is a hard operator boundary → **Option B**, and
  negotiate only the A6 `/dev/kvm` device plugin as the one minimal node ask for qemu speed.

Either way, `/dev/kvm` exposure (A6) is the single node-level concession that separates
"qemu works slowly" from "qemu works." That is the smallest question to put to the operator.
