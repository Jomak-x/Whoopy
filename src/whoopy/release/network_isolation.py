"""Validation shared by offline receipts that use macOS process isolation."""

from __future__ import annotations

import hashlib

MACOS_NETWORK_SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip \"localhost:*\"))
"""
MACOS_NETWORK_SANDBOX_PROFILE_SHA256 = hashlib.sha256(
    MACOS_NETWORK_SANDBOX_PROFILE.encode("utf-8")
).hexdigest()


def valid_macos_network_isolation_evidence(evidence: object) -> bool:
    """Accept only the exact pre-control, denial, and loopback proof contract."""

    if not isinstance(evidence, dict):
        return False
    if (
        evidence.get("schema_version") != 1
        or evidence.get("control_kind") != "pre_sandbox_reachability_then_in_sandbox_denial"
        or evidence.get("sandbox_executable") != "/usr/bin/sandbox-exec"
        or evidence.get("sandbox_profile_sha256") != MACOS_NETWORK_SANDBOX_PROFILE_SHA256
    ):
        return False
    control = evidence.get("external_control")
    denied = evidence.get("external_denial_probes")
    loopback = evidence.get("loopback_probe")
    if (
        not isinstance(control, dict)
        or control.get("schema_version") != 1
        or not isinstance(control.get("reachable_endpoints"), list)
        or not control["reachable_endpoints"]
        or not isinstance(denied, list)
        or not denied
        or not isinstance(loopback, dict)
        or loopback.get("host") != "127.0.0.1"
        or loopback.get("round_trip_passed") is not True
    ):
        return False
    reachable = {
        (item.get("host"), item.get("port"))
        for item in control["reachable_endpoints"]
        if isinstance(item, dict)
    }
    denied_endpoints = {
        (item.get("host"), item.get("port"))
        for item in denied
        if isinstance(item, dict)
        and item.get("connect_errno") in {1, 13}
        and item.get("errno_name") in {"EPERM", "EACCES"}
    }
    control_successes = {
        (item.get("host"), item.get("port"))
        for item in control.get("probes", [])
        if isinstance(item, dict) and item.get("connect_errno") == 0
    }
    return bool(reachable) and reachable == denied_endpoints and reachable <= control_successes
