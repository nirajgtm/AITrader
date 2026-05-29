"""Allow `python3 -m _providers --status` to work."""
import sys

from . import status


def main() -> int:
    if "--status" in sys.argv or len(sys.argv) == 1:
        for info in status():
            print(f"\n[{info['provider']}]")
            for k, v in info.items():
                if k == "provider":
                    continue
                print(f"  {k}: {v}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
