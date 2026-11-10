"""Resolve one identifier from the command line without network access."""

import argparse
import json

from .resolver import DidResolutionError, resolve


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve an Ed25519 did:key offline")
    parser.add_argument("did", help="bare base58btc Ed25519 did:key")
    args = parser.parse_args()
    try:
        document = resolve(args.did)
    except DidResolutionError as exc:
        parser.error(str(exc))
    print(json.dumps(document, indent=2))


if __name__ == "__main__":
    main()
