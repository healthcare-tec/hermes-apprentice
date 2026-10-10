#!/usr/bin/env python3
"""Verifica que os cenários versionados são reproduzíveis a partir do gerador.

Regenera os cenários em um diretório temporário e compara, arquivo por arquivo,
com o que está no repositório. O único campo ignorado é ``generated_at`` do
manifesto — o resto precisa ser idêntico byte a byte, senão o conjunto de
avaliação deixou de ser auditável.

Uso:
    python scripts/check_scenarios_reproducible.py [--scenarios scenarios]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

IGNORED_KEYS = {"generated_at"}


def strip_volatile(data: dict) -> dict:
    return {key: value for key, value in data.items() if key not in IGNORED_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default="scenarios")
    args = parser.parse_args()

    committed = Path(args.scenarios)
    if not committed.is_dir():
        print(f"[!] {committed} não existe", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "scenarios"
        subprocess.run(
            [sys.executable, "scripts/make_scenarios.py", "--out", str(fresh)],
            check=True,
            capture_output=True,
        )

        failures: list[str] = []
        for scenario_dir in sorted(p for p in fresh.iterdir() if p.is_dir()):
            name = scenario_dir.name
            target = committed / name
            if not target.is_dir():
                failures.append(f"{name}: ausente no repositório")
                continue

            fresh_events = (scenario_dir / "events.ndjson").read_text(encoding="utf-8")
            committed_events = (target / "events.ndjson").read_text(encoding="utf-8")
            if fresh_events != committed_events:
                failures.append(f"{name}: events.ndjson divergente")

            fresh_meta = strip_volatile(json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8")))
            committed_meta = strip_volatile(json.loads((target / "scenario.json").read_text(encoding="utf-8")))
            if fresh_meta != committed_meta:
                failures.append(f"{name}: scenario.json divergente")

        fresh_manifest = strip_volatile(json.loads((fresh / "manifest.json").read_text(encoding="utf-8")))
        committed_manifest = strip_volatile(
            json.loads((committed / "manifest.json").read_text(encoding="utf-8"))
        )
        if fresh_manifest != committed_manifest:
            failures.append("manifest.json divergente")

    if failures:
        for failure in failures:
            print(f"::error::cenário não reproduzível — {failure}")
        return 1
    print(f"[ok] {len(list(committed.glob('*/scenario.json')))} cenários reproduzidos byte a byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())