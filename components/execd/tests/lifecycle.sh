#!/bin/bash
# Copyright 2026 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOOTSTRAP="$ROOT_DIR/bootstrap.sh"
TESTDIR="$(mktemp -d)"
BOOTSTRAP_PID=""
cleanup() {
    if [ -n "$BOOTSTRAP_PID" ]; then
        kill -TERM "$BOOTSTRAP_PID" 2>/dev/null || true
        wait "$BOOTSTRAP_PID" 2>/dev/null || true
    fi
    rm -rf "$TESTDIR"
}
trap cleanup EXIT

assert_status_dir_empty() {
    leaked="$(ls -A "$STATUS_DIR")"
    if [ -n "$leaked" ]; then
        echo "FAIL: leaked lifecycle temp entries: $leaked" >&2
        exit 1
    fi
}

EXECD_STUB="$TESTDIR/execd"
cat > "$EXECD_STUB" <<'STUB'
#!/bin/sh
status_file=""
while [ "$#" -gt 0 ]; do
    case "$1" in
    --lifecycle-startup-status-file)
        if [ -z "${2:-}" ]; then
            echo "missing value for --lifecycle-startup-status-file" >&2
            exit 1
        fi
        status_file="$2"
        shift 2
        ;;
    --lifecycle-startup-status-file=*)
        status_file="${1#*=}"
        if [ -z "$status_file" ]; then
            echo "missing value for --lifecycle-startup-status-file" >&2
            exit 1
        fi
        shift
        ;;
    *)
        echo "unexpected argument: $1" >&2
        exit 1
        ;;
    esac
done
: "${EXECD_MARKER:?stub requires EXECD_MARKER}"
: "${EXECD_READY_MARKER:?stub requires EXECD_READY_MARKER}"
: "${SEQUENCE_FILE:?stub requires SEQUENCE_FILE}"
touch "$EXECD_MARKER"
printf 'execd-started\n' >> "$SEQUENCE_FILE"
if [ -n "$status_file" ]; then
    sleep 0.05
    touch "$EXECD_READY_MARKER"
    printf 'execd-ready\n' >> "$SEQUENCE_FILE"
    if [ "${EXECD_DIE_BEFORE_STATUS:-}" = "1" ]; then
        exit 17
    fi
    if [ -z "${OPEN_SANDBOX_LIFECYCLE:-}" ]; then
        test -n "${EXECD_LIFECYCLE_CONFIG:-}" && test -f "$EXECD_LIFECYCLE_CONFIG" || exit 1
    fi
    if [ "${PRESTART_BLOCK:-}" = "1" ]; then
        trap 'touch "$PRESTART_TERMINATED_MARKER"; exit 0' TERM INT
        touch "$PRESTART_MARKER"
        printf 'preStart\n' >> "$SEQUENCE_FILE"
        while true; do sleep 1; done
    fi
    touch "$PRESTART_MARKER"
    printf 'preStart\n' >> "$SEQUENCE_FILE"
    prestart_status="${PRESTART_EXIT_CODE:-0}"
    if [ -n "${PRESTART_STATUS_RAW+x}" ]; then
        printf '%s' "$PRESTART_STATUS_RAW" > "$status_file"
    else
        printf '%s\n' "$prestart_status" > "$status_file"
    fi
    if [ "${EXECD_STATUS_STAY_ALIVE:-}" = "1" ]; then
        trap 'touch "$STATUS_TERMINATED_MARKER"; exit 0' TERM INT
        while true; do sleep 1; done
    fi
    if [ "$prestart_status" -ne 0 ]; then
        exit "$prestart_status"
    fi
fi
trap 'exit 0' TERM INT
while true; do sleep 1; done
STUB
chmod +x "$EXECD_STUB"

USER_SCRIPT="$TESTDIR/user.sh"
cat > "$USER_SCRIPT" <<'USER'
#!/bin/sh
set -e
test -f "$PRESTART_MARKER"
test -f "$EXECD_READY_MARKER"
test -z "${OPEN_SANDBOX_LIFECYCLE:-}"
test -z "${EXECD_LIFECYCLE_CONFIG:-}"
test -f "$EXECD_MARKER"
touch "$USER_MARKER"
printf 'user\n' >> "$SEQUENCE_FILE"
USER
chmod +x "$USER_SCRIPT"

PRESTART_MARKER="$TESTDIR/prestart"
EXECD_MARKER="$TESTDIR/execd-started"
EXECD_READY_MARKER="$TESTDIR/execd-ready"
USER_MARKER="$TESTDIR/user-started"
SEQUENCE_FILE="$TESTDIR/sequence"
STATUS_DIR="$TESTDIR/status"
mkdir "$STATUS_DIR"
OPEN_SANDBOX_LIFECYCLE='{"preStart":{"command":["true"]}}' \
EXECD="$EXECD_STUB" \
PRESTART_MARKER="$PRESTART_MARKER" \
EXECD_MARKER="$EXECD_MARKER" \
EXECD_READY_MARKER="$EXECD_READY_MARKER" \
USER_MARKER="$USER_MARKER" \
SEQUENCE_FILE="$SEQUENCE_FILE" \
TMPDIR="$STATUS_DIR" \
BOOTSTRAP_CMD="$USER_SCRIPT" \
"$BOOTSTRAP"

test -f "$PRESTART_MARKER"
test -f "$EXECD_MARKER"
test -f "$EXECD_READY_MARKER"
test -f "$USER_MARKER"
test "$(cat "$SEQUENCE_FILE")" = "$(printf 'execd-started\nexecd-ready\npreStart\nuser')"
assert_status_dir_empty
echo "PASS: preStart completed before the user entrypoint"

rm -f "$PRESTART_MARKER" "$EXECD_MARKER" "$EXECD_READY_MARKER" "$USER_MARKER" "$SEQUENCE_FILE"
set +e
OPEN_SANDBOX_LIFECYCLE='{"preStart":{"command":["true"]}}' \
EXECD="$EXECD_STUB" \
PRESTART_EXIT_CODE=42 \
PRESTART_MARKER="$PRESTART_MARKER" \
EXECD_MARKER="$EXECD_MARKER" \
EXECD_READY_MARKER="$EXECD_READY_MARKER" \
USER_MARKER="$USER_MARKER" \
SEQUENCE_FILE="$SEQUENCE_FILE" \
TMPDIR="$STATUS_DIR" \
BOOTSTRAP_CMD="$USER_SCRIPT" \
"$BOOTSTRAP"
status=$?
set -e

test "$status" -eq 42
test -f "$EXECD_MARKER"
test -f "$EXECD_READY_MARKER"
test ! -f "$USER_MARKER"
assert_status_dir_empty
echo "PASS: preStart failure stops execd and prevents the user entrypoint from starting"

rm -f "$PRESTART_MARKER" "$EXECD_MARKER" "$EXECD_READY_MARKER" "$USER_MARKER" "$SEQUENCE_FILE"
PRESTART_TERMINATED_MARKER="$TESTDIR/prestart-terminated"
OPEN_SANDBOX_LIFECYCLE='{"preStart":{"command":["true"]}}' \
EXECD="$EXECD_STUB" \
PRESTART_BLOCK=1 \
PRESTART_MARKER="$PRESTART_MARKER" \
PRESTART_TERMINATED_MARKER="$PRESTART_TERMINATED_MARKER" \
EXECD_MARKER="$EXECD_MARKER" \
EXECD_READY_MARKER="$EXECD_READY_MARKER" \
USER_MARKER="$USER_MARKER" \
SEQUENCE_FILE="$SEQUENCE_FILE" \
TMPDIR="$STATUS_DIR" \
BOOTSTRAP_CMD="$USER_SCRIPT" \
"$BOOTSTRAP" &
BOOTSTRAP_PID=$!

i=0
while [ ! -f "$PRESTART_MARKER" ] && [ "$i" -lt 50 ]; do
    sleep 0.1
    i=$((i + 1))
done
test -f "$PRESTART_MARKER"
kill -TERM "$BOOTSTRAP_PID"
wait "$BOOTSTRAP_PID" || true
BOOTSTRAP_PID=""
test -f "$PRESTART_TERMINATED_MARKER"
test -f "$EXECD_MARKER"
test -f "$EXECD_READY_MARKER"
test ! -f "$USER_MARKER"
assert_status_dir_empty
echo "PASS: termination during preStart is forwarded to the hook"

rm -f "$PRESTART_MARKER" "$EXECD_MARKER" "$EXECD_READY_MARKER" "$USER_MARKER" "$SEQUENCE_FILE"
set +e
OPEN_SANDBOX_LIFECYCLE='{"preStart":{"command":["true"]}}' \
EXECD="$EXECD_STUB" \
EXECD_DIE_BEFORE_STATUS=1 \
PRESTART_MARKER="$PRESTART_MARKER" \
EXECD_MARKER="$EXECD_MARKER" \
EXECD_READY_MARKER="$EXECD_READY_MARKER" \
USER_MARKER="$USER_MARKER" \
SEQUENCE_FILE="$SEQUENCE_FILE" \
TMPDIR="$STATUS_DIR" \
BOOTSTRAP_CMD="$USER_SCRIPT" \
"$BOOTSTRAP"
status=$?
set -e

test "$status" -eq 17
test -f "$EXECD_MARKER"
test -f "$EXECD_READY_MARKER"
test ! -f "$PRESTART_MARKER"
test ! -f "$USER_MARKER"
assert_status_dir_empty
echo "PASS: execd exit before lifecycle status fails startup without leaking the status file"

rm -f "$PRESTART_MARKER" "$EXECD_MARKER" "$EXECD_READY_MARKER" "$USER_MARKER" "$SEQUENCE_FILE"

STATUS_TERMINATED_MARKER="$TESTDIR/status-terminated"
set +e
OPEN_SANDBOX_LIFECYCLE='{"preStart":{"command":["true"]}}' \
EXECD="$EXECD_STUB" \
PRESTART_STATUS_RAW=garbled \
EXECD_STATUS_STAY_ALIVE=1 \
STATUS_TERMINATED_MARKER="$STATUS_TERMINATED_MARKER" \
PRESTART_MARKER="$PRESTART_MARKER" \
EXECD_MARKER="$EXECD_MARKER" \
EXECD_READY_MARKER="$EXECD_READY_MARKER" \
USER_MARKER="$USER_MARKER" \
SEQUENCE_FILE="$SEQUENCE_FILE" \
TMPDIR="$STATUS_DIR" \
BOOTSTRAP_CMD="$USER_SCRIPT" \
"$BOOTSTRAP"
status=$?
set -e

test "$status" -eq 1
test -f "$STATUS_TERMINATED_MARKER"
test ! -f "$USER_MARKER"
assert_status_dir_empty
echo "PASS: malformed lifecycle status fails closed and terminates a still-running execd"

rm -f "$PRESTART_MARKER" "$EXECD_MARKER" "$EXECD_READY_MARKER" "$USER_MARKER" "$SEQUENCE_FILE"
PERSISTED_CONFIG="$TESTDIR/lifecycle.toml"
printf 'version = 1\n' > "$PERSISTED_CONFIG"
EXECD_LIFECYCLE_CONFIG="$PERSISTED_CONFIG" \
EXECD="$EXECD_STUB" \
PRESTART_MARKER="$PRESTART_MARKER" \
EXECD_MARKER="$EXECD_MARKER" \
EXECD_READY_MARKER="$EXECD_READY_MARKER" \
USER_MARKER="$USER_MARKER" \
SEQUENCE_FILE="$SEQUENCE_FILE" \
TMPDIR="$STATUS_DIR" \
BOOTSTRAP_CMD="$USER_SCRIPT" \
"$BOOTSTRAP"

test -f "$PRESTART_MARKER"
test -f "$EXECD_MARKER"
test -f "$EXECD_READY_MARKER"
test -f "$USER_MARKER"
assert_status_dir_empty
echo "PASS: persisted lifecycle config triggers preStart"

rm -f "$EXECD_MARKER" "$EXECD_READY_MARKER" "$USER_MARKER" "$SEQUENCE_FILE"
SANITIZE_USER_SCRIPT="$TESTDIR/sanitize-user.sh"
cat > "$SANITIZE_USER_SCRIPT" <<'USER'
#!/bin/sh
set -e
test -z "${OPEN_SANDBOX_LIFECYCLE:-}"
test -z "${EXECD_LIFECYCLE_CONFIG:-}"
touch "$USER_MARKER"
USER
chmod +x "$SANITIZE_USER_SCRIPT"
OPEN_SANDBOX_LIFECYCLE='' \
EXECD_LIFECYCLE_CONFIG="$TESTDIR/missing-lifecycle.toml" \
EXECD="$EXECD_STUB" \
EXECD_MARKER="$EXECD_MARKER" \
EXECD_READY_MARKER="$EXECD_READY_MARKER" \
USER_MARKER="$USER_MARKER" \
SEQUENCE_FILE="$SEQUENCE_FILE" \
BOOTSTRAP_CMD="$SANITIZE_USER_SCRIPT" \
"$BOOTSTRAP"

test -f "$USER_MARKER"
echo "PASS: internal lifecycle environment is stripped without a configured hook"
