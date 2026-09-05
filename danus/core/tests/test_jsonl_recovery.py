from pathlib import Path

from danus.core._util import append_jsonl, read_jsonl


def test_truncated_utf8_record_does_not_block_later_records(tmp_path: Path):
    encoded = "科".encode("utf-8")

    for cut in range(1, len(encoded)):
        path = tmp_path / f"ledger-{cut}.jsonl"
        append_jsonl(path, {"event": "before", "note": "中文"})
        with path.open("ab") as handle:
            handle.write(b'{"event":"STATE","note":"' + encoded[:cut])
        append_jsonl(path, {"event": "after", "note": "完整"})

        assert [row["event"] for row in read_jsonl(path)] == ["before", "after"]
