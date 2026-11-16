"""Container-local HTTP probe; no model call or external network access."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SERVICE = "match"
DEFAULT_PORT = 8102


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="/api/v1/" + SERVICE + "/health")
    parser.add_argument("--timeout", type=float, default=3)
    args = parser.parse_args()
    if not args.path.startswith("/") or args.path.startswith("//"):
        parser.error("--path must be a local API path")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    port = int(os.getenv(SERVICE.upper() + "_PORT", str(DEFAULT_PORT)))
    # A health probe must never inherit a corporate outbound HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:" + str(port) + args.path, timeout=args.timeout) as response:
            payload = json.load(response)
            if not isinstance(payload, dict):
                return 1
    except (OSError, ValueError, urllib.error.URLError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
