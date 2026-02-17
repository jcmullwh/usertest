from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_events_fallback_without_normalized_events(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parent.parent
    agent_adapters_src = package_root / "src"

    script = (
        "import builtins\n"
        "import importlib\n"
        "import json\n"
        "import sys\n"
        f"sys.path.insert(0, {str(agent_adapters_src)!r})\n"
        "real_import = builtins.__import__\n"
        "def blocked(name, *args, **kwargs):\n"
        "    if name == 'normalized_events' or name.startswith('normalized_events.'):\n"
        "        raise ImportError('blocked for fallback test')\n"
        "    return real_import(name, *args, **kwargs)\n"
        "builtins.__import__ = blocked\n"
        "events = importlib.import_module('agent_adapters.events')\n"
        "event = events.make_event('read_file', {'path': 'README.md'}, "
        "ts='2026-02-07T00:00:00+00:00')\n"
        "assert event['type'] == 'read_file'\n"
        "assert event['data']['path'] == 'README.md'\n"
        "assert event['ts'] == '2026-02-07T00:00:00+00:00'\n"
        "from pathlib import Path\n"
        "tmp = Path(sys.argv[1])\n"
        "events.write_events_jsonl(tmp, [event])\n"
        "loaded = list(events.iter_events_jsonl(tmp))\n"
        "assert loaded and loaded[0]['type'] == 'read_file'\n"
        "print(json.dumps(event, ensure_ascii=False))\n"
    )

    out_path = tmp_path / "events.jsonl"
    proc = subprocess.run(
        [sys.executable, "-c", script, str(out_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert '"type": "read_file"' in proc.stdout
