import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import oci
from scripts import hunt


def response(data):
    return oci.response.Response(200, {}, data, None)


def error(status=500, code="InternalError", message="Out of host capacity", headers=None):
    return oci.exceptions.ServiceError(status, code, headers or {}, message)


def instance(shape=hunt.X86, state="RUNNING", name="ubuntu-arm-free", compartment="tenant"):
    return oci.core.models.Instance(id="instance", shape=shape, lifecycle_state=state,
        display_name=name, compartment_id=compartment,
        shape_config=oci.core.models.InstanceShapeConfig(ocpus=2, memory_in_gbs=12))


def image(shape):
    return oci.core.models.Image(id="arm-image" if shape == hunt.ARM else "x86-image",
        display_name="Canonical-Ubuntu-24.04", operating_system="Canonical Ubuntu",
        operating_system_version="24.04", lifecycle_state="AVAILABLE", listing_type="NONE",
        size_in_mbs=47694)


class HunterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = hunt.Settings(tenancy="tenant", compartment="tenant", region="us-sanjose-1",
            subnet="subnet", name="ubuntu-arm-free", state_file=self.temp.name + "/state.json", max_attempts=3)
        self.compute, self.iam, self.network, self.storage = [Mock() for _ in range(4)]
        self.iam.list_region_subscriptions.return_value = response([NS(region_name="us-sanjose-1", is_home_region=True)])
        self.iam.list_compartments.return_value = response([])
        self.iam.list_availability_domains.return_value = response([NS(name="AD-1")])
        self.network.get_subnet.return_value = response(NS(prohibit_public_ip_on_vnic=False, availability_domain=None))
        self.compute.list_instances.return_value = response([])
        self.storage.list_volumes.return_value = response([])
        self.storage.list_boot_volumes.return_value = response([])
        self.compute.list_images.side_effect = lambda **kw: response([image(kw["shape"])])
        self.compute.list_shapes.return_value = response([NS(shape=hunt.ARM), NS(shape=hunt.X86)])
        self.clock = 0
        self.sleeps = []
        def sleep(seconds):
            self.clock += seconds
            self.sleeps.append(seconds)
        patches = [patch("scripts.hunt.time.monotonic", lambda: self.clock),
                   patch("scripts.hunt.time.sleep", sleep),
                   patch("scripts.hunt.random.uniform", return_value=1),
                   patch("scripts.hunt.log"), patch("scripts.hunt.output")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def hunter(self, **changes):
        return hunt.Hunter(replace(self.settings, **changes), self.compute, self.iam, self.network, self.storage)

    def test_arm_no_capacity_then_x86_success_uses_correct_image_and_fixed_shape(self):
        self.compute.launch_instance.side_effect = [error(), response(instance())]
        h = self.hunter()
        self.assertEqual(h.run(), 0)
        calls = self.compute.launch_instance.call_args_list
        arm, x86 = [c.args[0] for c in calls]
        self.assertEqual(arm.shape, hunt.ARM)
        self.assertEqual(x86.shape, hunt.X86)
        self.assertEqual(x86.source_details.image_id, "x86-image")
        self.assertIsNone(x86.shape_config)
        self.assertEqual(x86.source_details.boot_volume_size_in_gbs, 50)
        self.assertEqual(x86.source_details.boot_volume_vpus_per_gb, 10)
        self.assertEqual(h.result, "created")

    def test_all_capacity_failures_are_explicitly_reported(self):
        self.compute.launch_instance.side_effect = error()
        h = self.hunter()
        self.assertEqual(h.run(), 0)
        self.assertEqual(h.result, "no_capacity")
        self.assertEqual(h.stats["capacity"], 3)
        self.assertIsNone(h.state.pending)

    def test_running_provisioning_stopped_instances_prevent_duplicate(self):
        for state in ("RUNNING", "PROVISIONING", "STOPPED"):
            with self.subTest(state=state):
                self.compute.list_instances.return_value = response([instance(state=state)])
                self.compute.get_instance.return_value = response(instance())
                self.hunter().run()
                self.compute.launch_instance.assert_not_called()

    def test_list_instances_failure_never_launches(self):
        self.compute.list_instances.side_effect = error(403, "NotAuthorized", "denied")
        with self.assertRaises(oci.exceptions.ServiceError):
            self.hunter().run()
        self.compute.launch_instance.assert_not_called()

    def test_409_is_not_success_without_instance(self):
        self.compute.launch_instance.side_effect = error(409, "Conflict", "busy")
        with self.assertRaisesRegex(ValueError, "409 without"):
            self.hunter().run()
        self.assertEqual(self.compute.launch_instance.call_count, 1)

    def test_409_with_confirmed_instance_stops(self):
        self.compute.list_instances.side_effect = [response([]), response([instance()])]
        self.compute.launch_instance.side_effect = error(409, "Conflict", "busy")
        h = self.hunter()
        self.assertEqual(h.run(), 0)
        self.assertEqual(h.result, "existing")

    def test_auth_failure_stops_immediately(self):
        self.compute.launch_instance.side_effect = error(401, "NotAuthenticated", "bad key")
        with self.assertRaises(oci.exceptions.ServiceError):
            self.hunter().run()
        self.assertEqual(self.compute.launch_instance.call_count, 1)

    def test_throttling_honors_retry_after_and_keeps_slower_pace(self):
        self.compute.launch_instance.side_effect = [error(429, "TooManyRequests", "slow down", {"retry-after": "90"}),
                                                   error(), response(instance())]
        h = self.hunter()
        h.run()
        self.assertEqual(self.sleeps[0], 90)
        self.assertGreater(self.sleeps[1], 15)
        self.assertEqual(h.stats["throttled"], 1)

    def test_uncertain_network_request_reuses_payload_token_even_after_429(self):
        self.compute.launch_instance.side_effect = [oci.exceptions.RequestException("read timeout"),
            error(429, "TooManyRequests", "slow down"), response(instance(shape=hunt.ARM))]
        self.hunter().run()
        calls = self.compute.launch_instance.call_args_list
        self.assertEqual(len({c.kwargs["opc_retry_token"] for c in calls}), 1)
        self.assertEqual(len({c.args[0].shape for c in calls}), 1)

    def test_unconfirmed_request_is_resumed_after_restart(self):
        self.compute.launch_instance.side_effect = oci.exceptions.RequestException("timeout")
        h = self.hunter(max_attempts=1)
        with self.assertRaisesRegex(ValueError, "unconfirmed"):
            h.run()
        token = h.state.pending["token"]
        self.compute.launch_instance.side_effect = None
        self.compute.launch_instance.return_value = response(instance(shape=hunt.ARM))
        self.hunter(max_attempts=1).run()
        self.assertEqual(self.compute.launch_instance.call_args.kwargs["opc_retry_token"], token)

    def test_changed_payload_cannot_replace_unconfirmed_request(self):
        self.compute.launch_instance.side_effect = oci.exceptions.RequestException("timeout")
        with self.assertRaises(ValueError):
            self.hunter(max_attempts=1).run()
        with self.assertRaisesRegex(ValueError, "payload changed"):
            self.hunter(max_attempts=1, memory=8).run()
        self.assertEqual(self.compute.launch_instance.call_count, 1)

    def test_arm_quota_error_does_not_block_x86(self):
        self.compute.launch_instance.side_effect = [error(400, "LimitExceeded", "limit"), response(instance())]
        self.hunter().run()
        self.assertEqual(self.compute.launch_instance.call_args.args[0].shape, hunt.X86)

    def test_arm_allocation_full_still_allows_x86(self):
        self.compute.list_instances.return_value = response([instance(shape=hunt.ARM, name="other")])
        self.compute.launch_instance.return_value = response(instance())
        self.hunter().run()
        self.assertEqual(self.compute.launch_instance.call_args.args[0].shape, hunt.X86)

    def test_storage_in_child_compartment_blocks_new_boot_volume(self):
        self.iam.list_compartments.return_value = response([NS(id="child", lifecycle_state="ACTIVE")])
        self.storage.list_volumes.side_effect = lambda **kw: response(
            [NS(size_in_gbs=151, lifecycle_state="AVAILABLE")] if kw["compartment_id"] == "child" else [])
        with self.assertRaisesRegex(ValueError, "Free storage exhausted"):
            self.hunter().run()
        self.compute.launch_instance.assert_not_called()

    def test_only_home_region_is_allowed(self):
        self.iam.list_region_subscriptions.return_value = response([NS(region_name="us-ashburn-1", is_home_region=True)])
        with self.assertRaisesRegex(ValueError, "home region"):
            self.hunter().run()
        self.compute.launch_instance.assert_not_called()

    def test_ad_specific_subnet_restricts_rotation(self):
        self.iam.list_availability_domains.return_value = response([NS(name="AD-1"), NS(name="AD-2")])
        self.network.get_subnet.return_value = response(NS(prohibit_public_ip_on_vnic=False, availability_domain="AD-2"))
        self.compute.launch_instance.side_effect = error()
        self.hunter().run()
        self.assertEqual({c.args[0].availability_domain for c in self.compute.launch_instance.call_args_list}, {"AD-2"})

    def test_marketplace_image_is_rejected(self):
        paid = image(hunt.ARM)
        paid.listing_type = "COMMUNITY"
        self.compute.list_images.side_effect = None
        self.compute.list_images.return_value = response([paid])
        with self.assertRaisesRegex(ValueError, "free Ubuntu"):
            self.hunter().run()
        self.compute.launch_instance.assert_not_called()

    def test_waits_for_running_after_acceptance(self):
        self.compute.launch_instance.return_value = response(instance(state="PROVISIONING"))
        self.compute.get_instance.return_value = response(instance())
        h = self.hunter()
        h.run()
        self.assertEqual(h.result, "created")
        self.assertEqual(self.compute.launch_instance.call_count, 1)
        self.compute.get_instance.assert_called_once()

    def test_terminated_after_acceptance_is_failure(self):
        self.compute.launch_instance.return_value = response(instance(state="PROVISIONING"))
        self.compute.get_instance.return_value = response(instance(state="TERMINATED"))
        h = self.hunter()
        with self.assertRaises(ValueError):
            h.run()
        self.assertEqual(h.result, "instance_failed")
        self.assertEqual(self.compute.launch_instance.call_count, 1)

    def test_runtime_limit_applies_even_to_unlimited_attempts(self):
        self.compute.launch_instance.side_effect = error()
        h = self.hunter(max_attempts=0, max_runtime=60)
        h.run()
        self.assertLessEqual(self.clock, 60)
        self.assertLessEqual(self.compute.launch_instance.call_count, 4)

    def test_disallowed_shapes_or_over_free_arm_limits_fail(self):
        for kw in ({"shapes": ("VM.Standard.E4.Flex",)}, {"ocpus": 4}, {"memory": 24}, {"interval": 3}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.hunter(**kw)

    def test_empty_optional_env_uses_defaults(self):
        with patch.dict("os.environ", {"OCI_CLI_TENANCY": "tenant", "SUBNET_OCID": "subnet",
                                      "COMPARTMENT_OCID": "", "IMAGE_OCID": ""}, clear=True):
            self.assertEqual(hunt.Settings.from_env().compartment, "tenant")

    def test_main_configuration_failure_writes_error_result(self):
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hunt.main(), 1)
        hunt.output.assert_any_call("result", "configuration_error")

    def test_inventory_reads_all_pages_without_sdk_retry_wrapper(self):
        first = oci.response.Response(200, {"opc-next-page": "next"}, [instance(name="other")], None)
        self.compute.list_instances.side_effect = [first, response([instance()])]
        self.hunter().run()
        self.compute.launch_instance.assert_not_called()
        calls = self.compute.list_instances.call_args_list
        self.assertEqual(calls[1].kwargs["page"], "next")
        self.assertIs(calls[1].kwargs["retry_strategy"], hunt.NO_RETRY)

    def test_retry_token_near_expiry_requires_manual_inspection(self):
        h = self.hunter()
        details = h.details((hunt.ARM, "AD-1", "arm-image"))
        h.state.prepare(details)
        h.state.write(dict(h.state.pending, time=hunt.time.time() - 24 * 3600))
        with self.assertRaisesRegex(ValueError, "near expiry"):
            h.state.prepare(details)

    def test_micro_quota_full_still_allows_arm(self):
        self.compute.list_instances.return_value = response([instance(name="micro1"), instance(name="micro2")])
        self.compute.launch_instance.return_value = response(instance(shape=hunt.ARM))
        self.hunter().run()
        self.assertEqual(self.compute.launch_instance.call_args.args[0].shape, hunt.ARM)

    def test_server_503_reuses_token(self):
        self.compute.launch_instance.side_effect = [error(503, "ServiceUnavailable", "busy"), response(instance(shape=hunt.ARM))]
        self.hunter().run()
        calls = self.compute.launch_instance.call_args_list
        self.assertEqual(calls[0].kwargs["opc_retry_token"], calls[1].kwargs["opc_retry_token"])

    def test_ssh_key_is_injected_for_both_architectures(self):
        h = self.hunter(ssh_key="ssh-ed25519 public-key")
        for shape in hunt.FREE_SHAPES:
            self.assertEqual(h.details((shape, "AD-1", "image")).metadata["ssh_authorized_keys"], "ssh-ed25519 public-key")


if __name__ == "__main__":
    unittest.main()
