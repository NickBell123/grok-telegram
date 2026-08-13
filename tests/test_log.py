import json
from pathlib import Path
from grok_telegram.log import JsonlLogger


def test_appends_lines(tmp_path: Path):
    log = JsonlLogger(tmp_path / "log.jsonl", now_fn=lambda: "2026-01-01T00:00:00Z")
    log.write("inbound", chat_id="42", details={"text": "hi"})
    log.write("push", chat_id="42", details={"len": 5})
    lines = (tmp_path / "log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    assert e1 == {
        "ts": "2026-01-01T00:00:00Z",
        "kind": "inbound",
        "chat_id": "42",
        "details": {"text": "hi"},
    }
