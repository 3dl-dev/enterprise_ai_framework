#!/bin/bash
# Measure which aider edit format actually lands, for each model the gateway offers.
#
# Runs INSIDE a workspace pod:
#     kubectl -n enterprise-ai exec -i deploy/ws-baron -c ttyd -- bash -s < this-file
#
# Why this exists: aider chooses an edit format from a table keyed on model name, and our
# models reach it as `openai/<gateway name>`, which is in no table. It therefore falls
# back to `whole`. Nobody has checked whether these models can hold the tighter formats
# through our gateway, and if they cannot, editing fails constantly and the coding-camp
# plan does not work. This turns that unknown into a number.
#
# Every trial starts from a clean tree, states its own pass condition, and is judged by
# running the tests — not by whether aider claimed success.
set -uo pipefail

MODELS="${MODELS:-glm-5.2@deepinfra glm-4.7@deepinfra}"
FORMATS="${FORMATS:-whole diff udiff}"
TASKS="${TASKS:-fix-bug two-files}"
# One sample per cell tells you a format CAN work, never that it is reliable — and the
# failure mode worth measuring here is intermittent. Raise REPS when the answer matters.
REPS="${REPS:-1}"
# Streaming ON by default because that is what a person at the terminal gets. Turning it
# off is not a neutral change of plumbing: with --no-stream, glm-5.2 in `diff` format
# failed 4/4 while passing 4/4 streamed. See docs/design/dogfood-findings.md.
STREAM="${STREAM:-1}"
STREAM_FLAG=""; [[ "$STREAM" == "1" ]] || STREAM_FLAG="--no-stream"
TRIAL_DIR=/tmp/editformat
RESULTS="${RESULTS:-/tmp/editformat-results.tsv}"

rm -rf "$TRIAL_DIR"; mkdir -p "$TRIAL_DIR"
: > "$RESULTS"

seed_repo() {
    rm -rf "$TRIAL_DIR/repo"; mkdir -p "$TRIAL_DIR/repo"; cd "$TRIAL_DIR/repo"
    git init -q -b main
    git config user.email probe@workspace.local
    git config user.name probe
    cat > app.py <<'PY'
"""Sample project."""


def add(a, b):
    return a - b


def greet(name):
    return f"hello, {name}"
PY
    cat > test_app.py <<'PY'
from app import add, greet


def test_add():
    assert add(2, 3) == 5


def test_greet():
    assert greet("world") == "hello, world"
PY
    git add -A && git commit -qm seed
}

# task name | files aider is given | instruction | shell command that must succeed
run_trial() {
    local model="$1" fmt="$2" task="$3" files="$4" msg="$5" check="$6" rep="${7:-1}"
    seed_repo
    local log="$TRIAL_DIR/${model//[^a-z0-9]/_}-${fmt}-${task}-${rep}.log"
    timeout 420 aider \
        --model "openai/${model}" \
        --edit-format "$fmt" \
        --no-auto-commits --no-gitignore --no-check-update --no-show-model-warnings \
        --no-analytics $STREAM_FLAG --yes-always \
        --message "$msg" $files > "$log" 2>&1
    local aider_rc=$?

    local verdict=fail
    if [[ $aider_rc -eq 124 ]]; then
        verdict=timeout
    elif bash -c "$check" >/dev/null 2>&1; then
        verdict=pass
    fi

    # Aider's own complaints about the format, which distinguish "the model cannot hold
    # the format" from "the model held the format and got the code wrong".
    local malformed=0
    grep -qiE 'SEARCH/REPLACE block failed|did not find|malformed|UnifiedDiff|no filename provided|edit failed' "$log" && malformed=1
    local retries
    retries=$(grep -ciE 'retr|failed to match' "$log")

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$model" "$fmt" "$task" "$rep" "$verdict" "$malformed" "$retries" \
        | tee -a "$RESULTS"
}

for model in $MODELS; do
    for fmt in $FORMATS; do
        for rep in $(seq 1 "$REPS"); do
            for task in $TASKS; do
                case "$task" in
                    fix-bug)
                        run_trial "$model" "$fmt" "$task" "app.py" \
                            "add() must return the sum, not the difference. Fix it." \
                            "cd $TRIAL_DIR/repo && python -m pytest -q" "$rep" ;;
                    two-files)
                        run_trial "$model" "$fmt" "$task" "app.py test_app.py" \
                            "Add a function mul(a, b) to app.py that multiplies, and a test for it named test_mul in test_app.py. Do not change anything else." \
                            "cd $TRIAL_DIR/repo && grep -q 'def mul' app.py && grep -q 'def test_mul' test_app.py && python -m pytest -q -k 'mul'" "$rep" ;;
                esac
            done
        done
    done
done

echo
echo "model	format	task	rep	verdict	format_complaints	retry_lines"
cat "$RESULTS"
