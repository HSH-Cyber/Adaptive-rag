"""Validate and export an immutable demo policy snapshot; switch on restart."""

import argparse

from app.policies.engine import PolicyEngine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Manifest to validate; defaults to bundled demo")
    parser.add_argument("--destination", required=True, help="New snapshot directory")
    args = parser.parse_args()
    policy = PolicyEngine.load(args.source) if args.source else PolicyEngine.demo()
    print(policy.export(args.destination))


if __name__ == "__main__":
    main()
