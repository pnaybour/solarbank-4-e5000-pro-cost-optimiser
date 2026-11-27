"""Private subprocess transport. All input is provided through an anonymous pipe."""
import json
import sys
from .providers import ProviderError, _request


def main():
    try:
        data = json.loads(sys.stdin.buffer.read(16385))
        raw = _request(**data)
    except ProviderError as exc:
        sys.stdout.write(str(exc))
        return 2
    except Exception:
        sys.stdout.write("connection_failed")
        return 2
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
