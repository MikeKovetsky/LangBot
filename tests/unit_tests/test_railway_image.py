"""Railway and the local stack run one pinned LangBot release under the patched core files.

`latest` moved to 4.11 on 2026-10-14; the 2026-10-23 Railway rebuild pulled it and every chat failed.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'rockchin/langbot:'


def _railway_tag() -> str:
    lines = (ROOT / 'docker/railway/Dockerfile').read_text().splitlines()
    froms = [line.split()[1] for line in lines if line.startswith('FROM ')]
    assert len(froms) == 1 and froms[0].startswith(IMAGE)
    return froms[0].removeprefix(IMAGE)


def test_railway_base_is_a_release_tag():
    assert re.fullmatch(r'v\d+\.\d+\.\d+(@sha256:[0-9a-f]{64})?', _railway_tag())


def test_compose_runs_the_railway_base():
    services = yaml.safe_load((ROOT / 'docker/docker-compose.yaml').read_text())['services']
    images = {name: svc['image'] for name, svc in services.items()}

    assert images == {name: IMAGE + _railway_tag() for name in services}
