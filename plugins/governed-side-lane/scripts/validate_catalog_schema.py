#!/usr/bin/env python3
"""CI-only JSON Schema validation; jsonschema is not a runner dependency."""
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


def main():
    schema = json.loads((ROOT / 'config/routing-catalog.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    paths = [Path(value) for value in sys.argv[1:]] or [ROOT / 'config/routing-catalog.json']
    failed = False
    for path in paths:
        errors = sorted(validator.iter_errors(json.loads(path.read_text())), key=lambda error: str(error.path))
        for error in errors:
            print(f'{path}:{list(error.path)}: {error.message}')
        failed = failed or bool(errors)
        if not errors:
            print(f'{path}: schema valid')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
