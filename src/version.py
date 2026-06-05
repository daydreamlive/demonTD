"""Single source of truth for the demonTD version string + cloud User-Agent.

No TouchDesigner or third-party deps — safe to import from the pure HTTP
modules (queue_client, oauth) in both the TD runtime and unit tests.

User-Agent convention (shared with rtmg-vst, see daydreamlive/rtmg-vst#7):
`DaydreamDEMON-<CLIENT>/<ver>`. The cloud orchestrator (demon-public-demo)
tags each session by client from this header instead of the brittle
"no Origin header" heuristic. The VST sends `DaydreamDEMON-VST/b<build>`;
demonTD sends `DaydreamDEMON-TD/<ver>` on every cloud REST call
(queue/join, status, claim, extend, leave, and API-key validation).

Keep DEMON_TD_VERSION in step with the release tag. BUILD_MARKER in
demon_ext.py is the more detailed per-build fingerprint shown in the boot
log; this is the clean semver advertised on the wire.
"""

DEMON_TD_VERSION = "0.2.14"
USER_AGENT = f"DaydreamDEMON-TD/{DEMON_TD_VERSION}"
