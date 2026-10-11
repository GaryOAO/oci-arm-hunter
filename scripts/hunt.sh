#!/usr/bin/env bash
# Poll Oracle Cloud until an A1.Flex (ARM, Always Free) instance can be created.
#
# Exits 0 as soon as the instance is launched (or already exists).
# Keeps looping on "Out of host capacity" until MAX_ATTEMPTS / timeout.
#
# Required env (set as GitHub Actions secrets, or export locally):
#   OCI_CLI_USER        - user OCID
#   OCI_CLI_TENANCY     - tenancy OCID
#   OCI_CLI_FINGERPRINT - API key fingerprint
#   OCI_CLI_KEY_FILE    - path to the private API key (PEM)
# Optional:
#   OCI_CLI_REGION      - defaults to us-sanjose-1
#   AD                  - availability domain name, default pUWP:US-SANJOSE-1-AD-1
#   SUBNET_OCID         - subnet to attach
#   IMAGE_OCID          - image OCID (Ubuntu 24.04 aarch64)
#   INSTANCE_NAME       - default ubuntu-arm-free
#   OCPUS / MEMORY_GB   - default 2 / 12 (Always Free ceiling)
#   MAX_ATTEMPTS        - default 60
#   SLEEP_SECONDS       - default 45
set -uo pipefail

: "${OCI_CLI_USER:?OCI_CLI_USER is required}"
: "${OCI_CLI_TENANCY:?OCI_CLI_TENANCY is required}"
: "${OCI_CLI_FINGERPRINT:?OCI_CLI_FINGERPRINT is required}"
: "${OCI_CLI_KEY_FILE:?OCI_CLI_KEY_FILE is required}"

export OCI_CLI_REGION="${OCI_CLI_REGION:-us-sanjose-1}"
AD="${AD:-pUWP:US-SANJOSE-1-AD-1}"
SUBNET_OCID="${SUBNET_OCID:?SUBNET_OCID is required}"
IMAGE_OCID="${IMAGE_OCID:?IMAGE_OCID is required}"
INSTANCE_NAME="${INSTANCE_NAME:-ubuntu-arm-free}"
OCPUS="${OCPUS:-2}"
MEMORY_GB="${MEMORY_GB:-12}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-60}"
SLEEP_SECONDS="${SLEEP_SECONDS:-45}"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

# ---- Already created? -------------------------------------------------------
existing="$(oci compute instance list \
  --compartment-id "$OCI_CLI_TENANCY" \
  --display-name "$INSTANCE_NAME" \
  --lifecycle-state RUNNING \
  --query 'data[0].id' --raw-output 2>/dev/null || true)"

if [ -n "$existing" ] && [ "$existing" != "null" ]; then
  log "Instance '$INSTANCE_NAME' already exists: $existing"
  echo "instance_id=$existing" >> "${GITHUB_OUTPUT:-/dev/null}"
  echo "created=true" >> "${GITHUB_OUTPUT:-/dev/null}"
  exit 0
fi

log "Hunting for $OCPUS OCPU / ${MEMORY_GB}GB A1.Flex in $AD"
log "Subnet: $SUBNET_OCID"
log "Image:  $IMAGE_OCID"
log "Will try up to $MAX_ATTEMPTS times, ${SLEEP_SECONDS}s apart."

attempt=0
while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
  attempt=$((attempt + 1))
  log "--- attempt $attempt/$MAX_ATTEMPTS ---"

  out="$(oci compute instance launch \
    --availability-domain "$AD" \
    --compartment-id "$OCI_CLI_TENANCY" \
    --shape VM.Standard.A1.Flex \
    --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}" \
    --display-name "$INSTANCE_NAME" \
    --image-id "$IMAGE_OCID" \
    --subnet-id "$SUBNET_OCID" \
    --assign-public-ip true \
    --wait-for-state RUNNING \
    --wait-interval-seconds 10 \
    2>&1)" && rc=0 || rc=$?

  if [ "$rc" -eq 0 ]; then
    log "SUCCESS - instance launched."
    echo "$out" | tail -20
    id="$(echo "$out" | grep -o 'ocid1\.instance\.[a-z0-9.\-]*' | head -1 || true)"
    echo "instance_id=$id" >> "${GITHUB_OUTPUT:-/dev/null}"
    echo "created=true" >> "${GITHUB_OUTPUT:-/dev/null}"
    exit 0
  fi

  if echo "$out" | grep -qi 'out of host capacity\|OutOfCapacity\|InternalError'; then
    log "Still out of capacity. Waiting ${SLEEP_SECONDS}s..."
  elif echo "$out" | grep -qi 'LimitExceeded\|QuotaExceeded'; then
    log "FATAL: quota/limit exceeded - stopping."
    echo "$out" | tail -20
    exit 1
  elif echo "$out" | grep -qi 'already exists\|Conflict'; then
    log "Instance already exists (conflict). Treating as success."
    echo "created=true" >> "${GITHUB_OUTPUT:-/dev/null}"
    exit 0
  else
    log "Unexpected error:"
    echo "$out" | tail -25
  fi

  sleep "$SLEEP_SECONDS"
done

log "Gave up after $MAX_ATTEMPTS attempts - no capacity yet."
echo "created=false" >> "${GITHUB_OUTPUT:-/dev/null}"
exit 0
