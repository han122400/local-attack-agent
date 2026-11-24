"""Attach externally recorded HTTP/1 request and response bytes to a finished v4.6 report."""

import argparse
import json

from engine.http_audit import import_exchange
from engine.paths import RUNS_ROOT
from engine.session_log import LogFailure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("attempt_id")
    parser.add_argument("request_id", help="Unique ID from the independent recording")
    parser.add_argument("--request", required=True, help="Raw HTTP/1 request file outside the run store")
    parser.add_argument("--response", required=True, help="Matching raw HTTP/1 response file")
    parser.add_argument("--field", choices=("response_status", "baseline_status"),
                        help="Select only when this exact exchange supplied that observation")
    args = parser.parse_args()
    try:
        receipt = import_exchange(RUNS_ROOT, args.run_id, args.attempt_id, args.request_id,
                                  args.request, args.response, field=args.field)
    except (KeyError, TypeError):
        parser.error("Invalid stored report or HTTP audit receipt")
    except (ValueError, OSError, LogFailure) as error:
        parser.error(str(error))
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
