"""Operator-only, dependency-free preparation of the official UCI Air Quality ZIP.

Does not create a V8 project or stage any input. Extracts raw CSV bytes and
records source ZIP/CSV hashes. Run only once before stage-inputs.

Usage: python scripts/v8_prepare_uci_air_quality.py ARCHIVE_ZIP OUTPUT_DIRECTORY
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

SOURCE_URL = "https://archive.ics.uci.edu/static/public/360/air%2Bquality.zip"
SOURCE_PAGE = "https://archive.ics.uci.edu/dataset/360/air+quality"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(archive: Path, destination: Path):
    archive = archive.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(
            f"refusing to overwrite an existing input directory: {destination}")
    archive_bytes = archive.read_bytes()
    if not zipfile.is_zipfile(archive):
        raise ValueError("not a valid ZIP archive")
    with zipfile.ZipFile(archive) as zipped:
        found = [entry for entry in zipped.infolist()
                 if not entry.is_dir()
                 and Path(entry.filename).name == "AirQualityUCI.csv"]
        if len(found) != 1:
            raise ValueError("ZIP must contain exactly one AirQualityUCI.csv entry")
        entry = found[0]
        if not 100_000 <= entry.file_size <= 5_000_000:
            raise ValueError(f"unexpected CSV uncompressed bytes: {entry.file_size}")
        raw = zipped.read(entry)
    lines = raw.decode("latin-1").splitlines()
    if not lines or not (lines[0].startswith("Date;Time;")
                         and "CO(GT)" in lines[0]):
        raise ValueError("unexpected raw UCI CSV header; refusing silent conversion")
    rows = [line for line in lines[1:]
            if line.strip("; \t\r\n")]
    if not 9_000 <= len(rows) <= 10_000:
        raise ValueError(f"unexpected nonblank UCI record count: {len(rows)}")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "AirQualityUCI.csv").write_bytes(raw)
    origin = {
        "dataset": "UCI Air Quality, ID 360",
        "doi": "10.24432/C59K5F",
        "download_url": SOURCE_URL,
        "dataset_page": SOURCE_PAGE,
        "citation": "Vito, S. (2008). Air Quality [Dataset]. UCI Machine Learning Repository.",
        "zip_filename": archive.name,
        "zip_sha256": sha(archive_bytes),
        "archive_member": entry.filename,
        "csv_sha256": sha(raw),
        "csv_bytes": len(raw),
        "raw_nonblank_data_lines": len(rows),
        "parsing": "raw bytes preserved; parsing deferred to approved experiment",
        "authenticity_limit": "Local SHA-256 identity, not authentication against an official published checksum",
    }
    (destination / "SOURCE.json").write_text(
        json.dumps(origin, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps(origin, ensure_ascii=False, indent=2))
    print("PREPARED: unmodified CSV + SOURCE.json; ready for one-time V8 input staging")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: v8_prepare_uci_air_quality.py ZIP OUTPUT_DIR")
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
