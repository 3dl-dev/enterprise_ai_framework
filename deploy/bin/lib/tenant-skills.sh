#!/usr/bin/env bash
# ensure_tenant_skill_configmaps <ns> <configmap-prefix> <skills-dir>
#
# One ConfigMap PER SKILL directory under <skills-dir> (each holding that skill's
# SKILL.md as the single key "SKILL.md"), named "<configmap-prefix>-<skill-name>".
# enterpriseaiframework-6ff: the tenant's Agent Skills corpus (bundle/skills/ locally) is
# tenant content, not platform content — same principle as
# deploy/bin/lib/tenant-instructions.sh's house rules — so it never goes in an image, and
# this is the script that turns a directory of SKILL.md files into the ConfigMaps
# deploy/k8s/50-chat.yaml and deploy/k8s/61-workspace.template.yaml mount.
#
# WHY ONE CONFIGMAP PER SKILL, NOT ONE COMBINED ONE (a real, stated cost, not hidden):
# `kubectl create configmap --from-file=<dir>` does not recurse into subdirectories, so a
# single ConfigMap cannot hold "<name>/SKILL.md" for several skills without a hand-built
# `items[].path` volume mapping that has to be regenerated — and the Deployment's pod spec
# edited — every time a skill is added or removed. One ConfigMap per skill sidesteps that
# at the cost of a real one: adding a THIRD skill needs a new ConfigMap block AND a new
# volume + volumeMount entry in both k8s templates, not just a new directory under
# bundle/skills/. Unlike deploy/bin/lib/tenant-instructions.sh's single TENANT.md (whose
# ConfigMap needs no pod-spec change ever), this does not scale silently — if the corpus
# grows past a handful of skills, generating the volume/volumeMount list in
# deploy/bin/deploy.sh instead of hand-maintaining it in the YAML is the fix, and is worth
# its own rd item at that point rather than being built speculatively now for two skills.
#
# Every ConfigMap is mounted as a DIRECTORY (not subPath) in both templates, matching
# tenant-instructions.sh's reasoning: LibreChat's DEPLOYMENT_SKILLS_DIR loader only ever
# scans at boot regardless (dogfood-findings.md finding 41 — there is no watcher), so this
# buys nothing there, but the workspace side benefits: opencode's directory walk sees a
# content change without a pod restart, the same property AGENTS.md already has.
#
# `--dry-run=client -o yaml | kubectl apply -f -`: same idiom as tenant-instructions.sh,
# for the same reason — create-or-replace without a separate exists-check race, still a
# real request to the real API server on the apply half.
ensure_tenant_skill_configmaps() {
    local ns="$1" prefix="$2" skills_dir="$3"
    local found=0
    for skill_md in "$skills_dir"/*/SKILL.md; do
        [[ -f "$skill_md" ]] || continue
        found=1
        local skill_dir skill_name
        skill_dir="$(dirname "$skill_md")"
        skill_name="$(basename "$skill_dir")"
        kubectl -n "$ns" create configmap "${prefix}-${skill_name}" \
            --from-file=SKILL.md="$skill_md" \
            --dry-run=client -o yaml | kubectl apply -f - >/dev/null
        echo "    skill ${skill_name} (${prefix}-${skill_name}, updated)"
    done
    if [[ "$found" -eq 0 ]]; then
        echo "    no SKILL.md found under ${skills_dir} — no skill ConfigMaps created" >&2
    fi
}
