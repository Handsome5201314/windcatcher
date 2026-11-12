"""One explicit, synthetic-only Harness gateway check using the project key.

Run only after verifying the relay protocol and dedicated proxy lease. This
probe never reads a case and never prints credentials or model response text.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.probe_harness_relay import API_BASE, settings


IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
NETWORK = re.compile(r"[0-9a-f]{64}\Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="authorize one charged synthetic request")
    parser.add_argument("--action", action="store_true", help="test allowlisted action selection instead of advice")
    parser.add_argument("--image", required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--proxy-image", required=True)
    args = parser.parse_args()
    if not args.run:
        print("synthetic_probe=not_requested")
        return 2
    config = settings()
    if (config.get("WINDCATCHER_HARNESS_BASE_URL") != API_BASE or
            config.get("WINDCATCHER_HARNESS_MODEL") != "gpt-6.1-sol" or
            not config.get("DEEPSEEK_API_KEY") or
            not IMAGE.fullmatch(args.image) or
            not IMAGE.fullmatch(args.proxy_image) or
            not NETWORK.fullmatch(args.network)):
        print("synthetic_probe=configuration_invalid")
        return 2
    os.environ.update({
        "WINDCATCHER_HARNESS_ADVICE": "1",
        "WINDCATCHER_HARNESS_BASE_URL": API_BASE,
        "WINDCATCHER_HARNESS_MODEL": "gpt-6.1-sol",
        "WINDCATCHER_HARNESS_IMAGE": args.image,
        "WINDCATCHER_HARNESS_EGRESS_NETWORK_ID": args.network,
        "WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE": args.proxy_image,
        "DEEPSEEK_API_KEY": config["DEEPSEEK_API_KEY"],
    })
    if args.action:
        os.environ["WINDCATCHER_HARNESS_ACTIONS"] = "1"
    from pipeline import harness_advice

    summary = {
        "schema_version": 1,
        "case_id": "synthetic-harness-gateway",
        "state_sha256": "a" * 64,
        "stages": {name: "ok" for name in (
            "import", "anonymize", "qc", "segment", "surface",
            "cfd_prep", "cfd_run", "report",
        )},
        "baseline_ready": True,
        "numeric_ok": True,
        "eligible": True,
    }
    if args.action:
        summary["case_id"] = "synthetic-harness-action"
        summary["stages"].update({name: "pending" for name in (
            "segment", "surface", "cfd_prep", "cfd_run", "report",
        )})
        summary.update(baseline_ready=False, numeric_ok=None, eligible=None)
    try:
        result = (harness_advice.request_action(
            summary, ["segment_candidate"], "synthetic-probe")
            if args.action else harness_advice.request_advice(summary, "synthetic-probe"))
    except Exception as exc:
        reason = (exc.reason if isinstance(exc, harness_advice.AdviceUnavailable)
                  else "unexpected_exception")
        print("synthetic_probe=failed exception=" + type(exc).__name__ +
              " reason=" + reason)
        return 1
    metering = result.get("metering") or {}
    selected = result.get("action") if args.action else (result.get("next_step") or {}).get("code")
    print("synthetic_probe=success source=" + str(result.get("source")) +
          " next_step=" + str(selected) +
          " metering=" + str(metering.get("status")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
