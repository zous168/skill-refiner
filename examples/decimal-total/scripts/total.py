#!/usr/bin/env python3
"""Demonstration implementation containing two intentional defects."""
import json
import sys


def main():
    values = json.load(open(sys.argv[1], encoding="utf-8"))["amounts"]
    # Defect 1: an empty list should produce 0.00, not fail.
    if not values:
        print(json.dumps({"error": "empty list"}))
        return
    # Defect 2: truncation loses cents before adding.
    total = sum(int(float(value)) for value in values)
    print(json.dumps({"total": f"{total:.2f}"}))


if __name__ == "__main__":
    main()

