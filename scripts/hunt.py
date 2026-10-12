#!/usr/bin/env python3
"""Hunt for Oracle Cloud A1.Flex (ARM, Always Free) capacity.

Why this exists instead of the OCI CLI:

  `oci compute instance launch` retries internally when the region has no
  capacity, so a single call blocks for ~100 seconds before reporting
  "Out of host capacity". A run of N attempts therefore took ~N*100s,
  which is far too slow to poll on a schedule.

  Calling the API directly with a short socket timeout and an explicit
  NoneRetryStrategy turns each attempt into a few seconds, so the same
  wall-clock window covers far more attempts.

Exits 0 on success or on "still no capacity"; non-zero only on real
misconfiguration, so a scheduled run is not red just because AD-1 is full.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import oci
from oci.core import ComputeClient
from oci.core.models import (
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)
from oci.retry import NoneRetryStrategy


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"::error::{name} is required")
    return value or ""


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}", flush=True)


# --- configuration -----------------------------------------------------------

USER = env("OCI_CLI_USER", required=True)
TENANCY = env("OCI_CLI_TENANCY", required=True)
FINGERPRINT = env("OCI_CLI_FINGERPRINT", required=True)
KEY_FILE = env("OCI_CLI_KEY_FILE", required=True)
REGION = env("OCI_CLI_REGION", "us-sanjose-1")

AD = env("AD", "pUWP:US-SANJOSE-1-AD-1")
SUBNET_OCID = env("SUBNET_OCID", required=True)
IMAGE_OCID = env("IMAGE_OCID", required=True)
INSTANCE_NAME = env("INSTANCE_NAME", "ubuntu-arm-free")

OCPUS = float(env("OCPUS", "2"))
MEMORY_GB = float(env("MEMORY_GB", "12"))

MAX_ATTEMPTS = int(env("MAX_ATTEMPTS", "12"))
SLEEP_SECONDS = float(env("SLEEP_SECONDS", "2"))
# Per-request read timeout. Short on purpose: when capacity is missing the
# service answers quickly once the client stops retrying.
CALL_TIMEOUT = float(env("CALL_TIMEOUT", "15"))
# 429 backoff. Oracle throttles bursts of create calls, so ease off rather
# than turning the rest of the run into 429s.
THROTTLE_BASE = float(env("THROTTLE_BASE", "5"))
THROTTLE_MAX = float(env("THROTTLE_MAX", "30"))

config = {
    "user": USER,
    "tenancy": TENANCY,
    "fingerprint": FINGERPRINT,
    "key_file": KEY_FILE,
    "region": REGION,
    # No SDK-level retries: they are what made each attempt take ~100s.
    "retry_strategy": NoneRetryStrategy(),
}

# The SDK defaults to (connect 10s, read 60s). A 60s read timeout is exactly
# why the CLI blocked ~100s per attempt when AD-1 had no capacity. Pass a
# short (connect, read) tuple on the client instead.
CLIENT_TIMEOUT = (5.0, CALL_TIMEOUT)

# --- helpers -----------------------------------------------------------------


def output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def existing_instance_id(compute: ComputeClient) -> str | None:
    """Return the id of a RUNNING instance with our display name, if any."""
    try:
        response = compute.list_instances(
            compartment_id=TENANCY,
            display_name=INSTANCE_NAME,
            lifecycle_state="RUNNING",
        )
    except Exception as error:  # noqa: BLE001 - surfaced below
        log(f"Could not list instances: {error}")
        return None

    for instance in response.data:
        if instance.display_name == INSTANCE_NAME:
            return instance.id
    return None


def is_capacity_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "out of host capacity" in lowered
        or "outofcapacity" in lowered
        or "capacity" in lowered and "host" in lowered
    )


def is_limit_error(message: str) -> bool:
    lowered = message.lower()
    return "limitexceeded" in lowered or "quota" in lowered


def is_throttle_error(message: str) -> bool:
    """429 TooManyRequests. Expected when polling fast; back off and retry."""
    return "toomanyrequests" in message.lower() or "'status': 429" in message


# --- main --------------------------------------------------------------------

compute = ComputeClient(config, timeout=CLIENT_TIMEOUT)

already = existing_instance_id(compute)
if already:
    log(f"Instance '{INSTANCE_NAME}' already exists: {already}")
    output("instance_id", already)
    output("created", "true")
    sys.exit(0)

log(f"Hunting for {OCPUS:g} OCPU / {MEMORY_GB:g}GB A1.Flex in {AD}")
log(f"Region {REGION}, image {IMAGE_OCID}")
log(f"Up to {MAX_ATTEMPTS} attempts, {CALL_TIMEOUT:g}s per request, {SLEEP_SECONDS:g}s between.")

details = LaunchInstanceDetails(
    availability_domain=AD,
    compartment_id=TENANCY,
    display_name=INSTANCE_NAME,
    shape="VM.Standard.A1.Flex",
    shape_config=LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEMORY_GB),
    source_details=InstanceSourceViaImageDetails(
        source_type="image",
        image_id=IMAGE_OCID,
    ),
    create_vnic_details=CreateVnicDetails(
        subnet_id=SUBNET_OCID,
        assign_public_ip=True,
    ),
)

throttle_strikes = 0

for attempt in range(1, MAX_ATTEMPTS + 1):
    # A fresh retry token per attempt: it lets the service deduplicate a
    # genuinely repeated request, and must not be reused across attempts
    # for different logical creates.
    retry_token = str(uuid.uuid4())
    started = time.time()
    sleep_for = SLEEP_SECONDS
    try:
        response = compute.launch_instance(
            details,
            opc_retry_token=retry_token,
            retry_strategy=NoneRetryStrategy(),
        )
        elapsed = time.time() - started
        instance_id = response.data.id
        log(f"SUCCESS after {elapsed:.1f}s - instance {instance_id}")
        output("instance_id", instance_id)
        output("created", "true")
        sys.exit(0)
    except Exception as error:  # noqa: BLE001 - we classify by message
        elapsed = time.time() - started
        message = str(error)
        status = getattr(error, "status", None)

        if is_capacity_error(message):
            log(f"attempt {attempt}/{MAX_ATTEMPTS}: no capacity ({elapsed:.1f}s)")
            sleep_for = SLEEP_SECONDS
        elif is_throttle_error(message):
            # Oracle throttles rapid create attempts. Back off multiplicatively
            # rather than hammering, or every later attempt gets 429 too.
            throttle_strikes += 1
            sleep_for = min(THROTTLE_BASE * (2 ** (throttle_strikes - 1)), THROTTLE_MAX)
            log(f"attempt {attempt}/{MAX_ATTEMPTS}: throttled (429), backing off {sleep_for:.0f}s")
        elif is_limit_error(message):
            log(f"::error::Quota limit hit, stopping: {message}")
            output("created", "false")
            sys.exit(1)
        elif status == 409:
            log("Instance appears to already exist (409). Treating as success.")
            output("created", "true")
            sys.exit(0)
        else:
            log(f"attempt {attempt}/{MAX_ATTEMPTS}: unexpected error ({elapsed:.1f}s): {message}")
            sleep_for = SLEEP_SECONDS

    if attempt < MAX_ATTEMPTS:
        time.sleep(sleep_for)

log(f"No capacity after {MAX_ATTEMPTS} attempts.")
output("created", "false")
sys.exit(0)
