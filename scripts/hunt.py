#!/usr/bin/env python3
"""Launch one Always Free Ubuntu VM (ARM or x86), then stop.

Only documented Always Free shapes are allowed. Capacity failures rotate
placements; throttling slows the entire loop. An ambiguous launch keeps its
payload and retry token on disk, including across scheduled Actions runs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
import time
import uuid
from dataclasses import dataclass

import oci
from oci.core.models import (
    CreateVnicDetails, InstanceSourceViaImageDetails,
    LaunchInstanceDetails, LaunchInstanceShapeConfigDetails,
)
from oci.retry import NoneRetryStrategy

ARM = "VM.Standard.A1.Flex"
X86 = "VM.Standard.E2.1.Micro"
FREE_SHAPES = (ARM, X86)
BOOT_GB = 50
NO_RETRY = NoneRetryStrategy()


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}", flush=True)


def output(key, value):
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def env(name, default="", required=False):
    value = (os.environ.get(name) or default).strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass
class Settings:
    tenancy: str
    compartment: str
    region: str
    subnet: str
    name: str
    shapes: tuple[str, ...] = FREE_SHAPES
    ad: str = "auto"
    arm_image: str = ""
    x86_image: str = ""
    ssh_key: str = ""
    ocpus: float = 2
    memory: float = 12
    max_attempts: int = 0
    max_runtime: float = 3300
    interval: float = 15
    state_file: str = ".hunt-state/request.json"

    def validate(self):
        if not self.shapes or set(self.shapes) - set(FREE_SHAPES):
            raise ValueError(f"SHAPES must contain only {FREE_SHAPES}")
        values = (self.ocpus, self.memory, self.max_runtime, self.interval)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Numeric settings must be finite")
        if not 1 <= self.ocpus <= 2 or not 1 <= self.memory <= 12:
            raise ValueError("ARM must stay within 1–2 OCPU / 1–12 GB")
        if self.max_attempts < 0 or not 1 <= self.max_runtime <= 3300 or self.interval < 10:
            raise ValueError("MAX_ATTEMPTS >= 0, runtime 1–3300s, sleep >= 10s required")

    @classmethod
    def from_env(cls):
        tenancy = env("OCI_CLI_TENANCY", required=True)
        settings = cls(
            tenancy=tenancy, compartment=env("COMPARTMENT_OCID", tenancy),
            region=env("OCI_CLI_REGION", "us-sanjose-1"),
            subnet=env("SUBNET_OCID", required=True),
            name=env("INSTANCE_NAME", "ubuntu-arm-free"),
            shapes=tuple(dict.fromkeys(env("SHAPES", ",".join(FREE_SHAPES)).split(","))),
            ad=env("AD", "auto"), arm_image=env("IMAGE_OCID"),
            x86_image=env("X86_IMAGE_OCID"), ssh_key=env("SSH_PUBLIC_KEY"),
            ocpus=float(env("OCPUS", "2")), memory=float(env("MEMORY_GB", "12")),
            max_attempts=int(env("MAX_ATTEMPTS", "0")),
            max_runtime=float(env("MAX_RUNTIME_SECONDS", "3300")),
            interval=float(env("SLEEP_SECONDS", "15")),
            state_file=env("STATE_FILE", ".hunt-state/request.json"),
        )
        settings.shapes = tuple(x.strip() for x in settings.shapes)
        settings.validate()
        return settings


def all_items(method, *args, **kwargs):
    # SDK pagination wraps each page with DEFAULT_RETRY_STRATEGY even when
    # the operation gets NoneRetryStrategy. Keep our bounded requests explicit.
    items = []
    while True:
        response = method(*args, retry_strategy=NO_RETRY, **kwargs)
        items.extend(response.data)
        page = response.headers.get("opc-next-page")
        if not page:
            return items
        if page == kwargs.get("page"):
            raise ValueError("OCI pagination repeated a page token")
        kwargs["page"] = page


def error_kind(error):
    """Classify structured OCI errors before falling back to message text."""
    status = getattr(error, "status", None)
    code = getattr(error, "code", "").lower()
    message = getattr(error, "message", str(error)).lower()
    if status == 429 or code == "toomanyrequests":
        return "throttled"
    if code in {"limitexceeded", "quotaexceeded"} or "quota" in message:
        return "quota"
    if status in {400, 401, 403, 404}:
        return "fatal"
    if "out of host capacity" in message or code in {"outofcapacity", "outofhostcapacity"}:
        return "capacity"
    if status == 409:
        return "conflict"
    if isinstance(error, oci.exceptions.RequestException) or status in {500, 502, 503, 504}:
        return "transient"
    return "fatal"


class RequestState:
    def __init__(self, path):
        self.path = Path(path)
        self.pending = json.loads(self.path.read_text()).get("pending") if self.path.exists() else None

    def write(self, pending):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pending": pending}), encoding="utf-8")
        tmp.replace(self.path)
        self.pending = pending

    def prepare(self, details):
        digest = hashlib.sha256(json.dumps(oci.util.to_dict(details), sort_keys=True).encode()).hexdigest()
        if self.pending:
            if self.pending["digest"] != digest:
                raise ValueError("Unresolved launch payload changed; inspect the previous request before changing configuration")
            if time.time() - self.pending["time"] >= 23 * 3600:
                raise ValueError("Unresolved retry token is near expiry; inspect OCI before issuing another launch")
        else:
            self.write(dict(digest=digest, token=str(uuid.uuid4()), time=time.time(), uncertain=False,
                            shape=details.shape, ad=details.availability_domain))
        return self.pending["token"]


class Hunter:
    def __init__(self, settings, compute, identity, network, storage):
        settings.validate()
        self.s = settings
        self.compute, self.identity, self.network, self.storage = compute, identity, network, storage
        self.state = RequestState(settings.state_file)
        self.stats = dict(attempts=0, capacity=0, throttled=0, transient=0, quota=0)
        self.result = "error"
        self.delay = settings.interval
        self.compartments = []
        self.all_ads = []

    def inventory(self):
        # Read errors propagate: never create if duplicate/cost checks failed.
        instances = []
        volume_gb = 0
        for compartment in self.compartments:
            instances.extend(all_items(self.compute.list_instances, compartment_id=compartment))
            volumes = all_items(self.storage.list_volumes, compartment_id=compartment)
            for ad in self.all_ads:
                volumes += all_items(self.storage.list_boot_volumes,
                                     availability_domain=ad, compartment_id=compartment)
            volume_gb += sum(v.size_in_gbs for v in volumes if v.lifecycle_state != "TERMINATED")
        return [i for i in instances if i.lifecycle_state != "TERMINATED"], volume_gb

    def existing(self, instances):
        matches = [i for i in instances if i.display_name == self.s.name]
        if len(matches) > 1:
            raise ValueError("Multiple instances with the target name; inspect OCI before continuing")
        if matches:
            if matches[0].shape not in FREE_SHAPES:
                raise ValueError("Target name is already used by a different shape")
            return matches[0]
        return None

    def eligible_shapes(self, instances, volume_gb):
        if volume_gb + BOOT_GB > 200:
            raise ValueError(f"Free storage exhausted: {volume_gb} GB used + {BOOT_GB} GB boot > 200 GB")
        arm = [i for i in instances if i.shape == ARM]
        micro = [i for i in instances if i.shape == X86]
        ocpus = sum(i.shape_config.ocpus for i in arm)
        memory = sum(i.shape_config.memory_in_gbs for i in arm)
        allowed = [shape for shape in self.s.shapes if
                   (shape == ARM and ocpus + self.s.ocpus <= 2 and memory + self.s.memory <= 12)
                   or (shape == X86 and len(micro) < 2)]
        if not allowed:
            raise ValueError("No requested shape fits the remaining Always Free compute allocation")
        return allowed

    def image(self, shape):
        configured = self.s.arm_image if shape == ARM else self.s.x86_image
        if configured:
            image = self.compute.get_image(configured, retry_strategy=NO_RETRY).data
            compatible = all_items(self.compute.list_image_shape_compatibility_entries, configured)
            if not any(x.shape == shape for x in compatible):
                raise ValueError(f"Configured image does not support {shape}")
        else:
            images = all_items(self.compute.list_images, compartment_id=self.s.compartment,
                               shape=shape, operating_system="Canonical Ubuntu",
                               sort_by="TIMECREATED", sort_order="DESC")
            images = [i for i in images if i.compartment_id is None and i.lifecycle_state == "AVAILABLE"
                      and i.operating_system_version in {"22.04", "24.04"}]
            if not images:
                raise ValueError(f"No free Ubuntu 22.04/24.04 platform image for {shape}")
            image = images[0]
        if image.compartment_id is not None or image.operating_system != "Canonical Ubuntu" or image.listing_type not in {None, "NONE"}:
            raise ValueError("Only free Ubuntu platform images are allowed (no custom/Marketplace images)")
        if image.lifecycle_state != "AVAILABLE" or image.size_in_mbs is None or image.size_in_mbs > BOOT_GB * 1024:
            raise ValueError("Image is unavailable or exceeds the 50 GB boot budget")
        log(f"{shape}: {image.display_name} ({image.id})")
        return image.id

    def preflight(self):
        subscriptions = all_items(self.identity.list_region_subscriptions, self.s.tenancy)
        if not any(r.region_name == self.s.region and r.is_home_region for r in subscriptions):
            raise ValueError("Always Free creation must use the tenancy's home region")
        # ANY lists the whole tree and fails if the caller lacks inspect access.
        compartments = all_items(self.identity.list_compartments, self.s.tenancy,
                                  compartment_id_in_subtree=True, access_level="ANY")
        self.compartments = [self.s.tenancy] + [c.id for c in compartments if c.lifecycle_state == "ACTIVE"]
        if self.s.compartment not in self.compartments:
            raise ValueError("Target compartment is not active in this tenancy")
        self.all_ads = [a.name for a in all_items(self.identity.list_availability_domains, self.s.tenancy)]
        subnet = self.network.get_subnet(self.s.subnet, retry_strategy=NO_RETRY).data
        if subnet.prohibit_public_ip_on_vnic:
            raise ValueError("A public subnet is required for this hunter")
        ads = self.all_ads if self.s.ad.lower() == "auto" else self.s.ad.split(",")
        ads = [a.strip() for a in ads]
        if not ads or set(ads) - set(self.all_ads):
            raise ValueError("AD must contain valid availability domains of this tenancy/region")
        if subnet.availability_domain:
            ads = [a for a in ads if a == subnet.availability_domain]
        if not ads:
            raise ValueError("No requested AD matches the subnet")
        instances, volume_gb = self.inventory()
        if instance := self.existing(instances):
            return [], instance
        candidates = []
        for shape in self.eligible_shapes(instances, volume_gb):
            image_id = self.image(shape)
            for ad in ads:
                shapes = all_items(self.compute.list_shapes, compartment_id=self.s.compartment,
                                   availability_domain=ad, image_id=image_id)
                if any(x.shape == shape for x in shapes):
                    candidates.append((shape, ad, image_id))
        if not candidates:
            raise ValueError("No Always Free shape is supported by the requested ADs/images")
        log(f"Free storage: {volume_gb}/200 GB; placements: {[(s, a) for s, a, _ in candidates]}")
        return candidates, None

    def details(self, candidate):
        shape, ad, image = candidate
        return LaunchInstanceDetails(
            availability_domain=ad, compartment_id=self.s.compartment, display_name=self.s.name,
            shape=shape,
            shape_config=LaunchInstanceShapeConfigDetails(ocpus=self.s.ocpus, memory_in_gbs=self.s.memory)
            if shape == ARM else None,  # E2.1.Micro is fixed-size; ARM settings must not leak into it.
            source_details=InstanceSourceViaImageDetails(
                image_id=image, boot_volume_size_in_gbs=BOOT_GB, boot_volume_vpus_per_gb=10),
            create_vnic_details=CreateVnicDetails(subnet_id=self.s.subnet, assign_public_ip=True),
            metadata={"ssh_authorized_keys": self.s.ssh_key} if self.s.ssh_key else None,
        )

    def finish_instance(self, instance, deadline, existing=False):
        self.state.write(None)
        output("instance_id", instance.id)
        output("shape", instance.shape)
        output("created", "true")
        # Acceptance is not the same as RUNNING. Never issue a second create.
        wait_deadline = min(deadline, time.monotonic() + 600)
        while instance.lifecycle_state in {"PROVISIONING", "STARTING"} and time.monotonic() + 15 < wait_deadline:
            time.sleep(15)
            instance = self.compute.get_instance(instance.id, retry_strategy=NO_RETRY).data
        output("lifecycle_state", instance.lifecycle_state)
        if instance.lifecycle_state in {"TERMINATED", "TERMINATING"}:
            self.result = "instance_failed"
            raise ValueError(f"Instance entered {instance.lifecycle_state}; inspect its boot volume before retrying")
        self.result = ("existing" if existing else "created") if instance.lifecycle_state == "RUNNING" else "pending_or_stopped"
        log(f"Instance {instance.id}: {instance.shape}, {instance.lifecycle_state}; result={self.result}")
        return 0

    def sleep(self, deadline, minimum=0):
        delay = max(minimum, self.delay * random.uniform(1, 1.2))
        time.sleep(max(0, min(delay, deadline - time.monotonic())))

    def run(self):
        deadline = time.monotonic() + self.s.max_runtime
        candidates, instance = self.preflight()
        if instance:
            return self.finish_instance(instance, deadline, existing=True)
        if self.state.pending:
            candidates = sorted(candidates, key=lambda c: (c[0], c[1]) !=
                                (self.state.pending["shape"], self.state.pending["ad"]))
        index = 0
        next_inventory = time.monotonic() + 300
        disabled = set()
        last_kind = None
        while time.monotonic() + 20 < deadline and (not self.s.max_attempts or self.stats["attempts"] < self.s.max_attempts):
            if time.monotonic() >= next_inventory or last_kind in {"transient", "conflict"}:
                instances, volume_gb = self.inventory()
                if instance := self.existing(instances):
                    return self.finish_instance(instance, deadline, existing=True)
                allowed = self.eligible_shapes(instances, volume_gb)
                disabled.update(set(FREE_SHAPES) - set(allowed))
                next_inventory = time.monotonic() + 300
            available = [c for c in candidates if c[0] not in disabled]
            if not available:
                self.result = "quota_blocked"
                raise ValueError("All free shapes exhausted their allocation/service quota")
            candidate = available[index % len(available)]
            details = self.details(candidate)
            token = self.state.prepare(details)
            self.stats["attempts"] += 1
            try:
                response = self.compute.launch_instance(details, opc_retry_token=token, retry_strategy=NO_RETRY)
            except Exception as error:
                kind = error_kind(error)
                last_kind = kind
                if kind == "fatal":
                    raise
                if kind == "conflict":
                    # A 409 alone proves nothing. Check inventory once, otherwise fail.
                    instances, _ = self.inventory()
                    if instance := self.existing(instances):
                        return self.finish_instance(instance, deadline, existing=True)
                    raise ValueError(f"409 without a confirmed instance: {error}") from error
                self.stats[kind] += 1
                retry_after = 0
                uncertain = self.state.pending.get("uncertain", False)
                if kind == "quota" and uncertain:
                    raise ValueError("Quota error on an unresolved launch; keeping its token for inspection") from error
                if kind == "capacity" or (kind in {"quota", "throttled"} and not uncertain):
                    self.state.write(None)  # Explicit rejection: this request did not create a VM.
                    index += 1
                elif kind == "transient":
                    self.state.write(dict(self.state.pending, uncertain=True))
                if kind == "capacity":
                    self.delay = max(self.s.interval, self.delay * 0.95)
                elif kind == "quota":
                    disabled.add(candidate[0])  # ARM quota must not suppress a free x86 placement.
                else:
                    self.delay = min(120, max(30, self.delay * 2))
                    headers = getattr(error, "headers", {}) or {}
                    try:
                        retry_after = float(headers.get("retry-after", 0))
                    except (ValueError, TypeError):
                        pass
                log(f"attempt {self.stats['attempts']}: {candidate[0]} in {candidate[1]}: {kind}; "
                    f"next interval >= {max(self.delay, retry_after):.0f}s")
                if kind != "capacity":
                    log(str(error))
                if not self.s.max_attempts or self.stats["attempts"] < self.s.max_attempts:
                    self.sleep(deadline, minimum=retry_after)
            else:
                return self.finish_instance(response.data, deadline)
        if self.state.pending:
            self.result = "unconfirmed"
            raise ValueError("Launch result remains unconfirmed; preserved payload/token for the next run")
        self.result = "no_capacity" if self.stats["capacity"] else "throttled" if self.stats["throttled"] else "window_expired"
        log(f"Hunt window finished: result={self.result}, stats={self.stats}")
        return 0


def main():
    hunter = None
    output("created", "false")
    try:
        settings = Settings.from_env()
        config = dict(user=env("OCI_CLI_USER", required=True), tenancy=settings.tenancy,
                      fingerprint=env("OCI_CLI_FINGERPRINT", required=True),
                      key_file=env("OCI_CLI_KEY_FILE", required=True), region=settings.region)
        kwargs = dict(timeout=(5, 15), retry_strategy=NO_RETRY)
        hunter = Hunter(settings, oci.core.ComputeClient(config, **kwargs),
                        oci.identity.IdentityClient(config, **kwargs),
                        oci.core.VirtualNetworkClient(config, **kwargs),
                        oci.core.BlockstorageClient(config, **kwargs))
        return hunter.run()
    except Exception as error:
        print(f"::error::{error}", flush=True)
        return 1
    finally:
        output("result", hunter.result if hunter else "configuration_error")
        if hunter:
            for key, value in hunter.stats.items():
                output(key, value)


if __name__ == "__main__":
    sys.exit(main())
