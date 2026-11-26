"""
File-based storage (no database).

data/
  campaigns/<CAMPAIGN_ID>/campaign.json   all rows, attempts, results, result files written
  campaigns/<CAMPAIGN_ID>/input.csv       the client's original file
  campaigns/<CAMPAIGN_ID>/outputs/        a copy of every result file written to the SFTP folder
  state/status.json                       heartbeats, last SFTP folder check, last call result
  state/file_log.jsonl                    every file picked up from the SFTP folder and what happened to it
  state/drop_files.json                   pickups in progress (makes them restart-safe)
  state/counter.json                      campaign number sequence
  locks/                                  lock files (several processes share these files)

Writes are atomic (temp file + rename) and guarded by file locks, so the
server, the scheduler and the dashboard can work on the same files safely.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from filelock import FileLock

from .config import settings
from .timeutil import LOCAL_TZ, to_local, utcnow

DATA = settings.data_dir
CAMPAIGNS = DATA / "campaigns"
STATE = DATA / "state"
LOCKS = DATA / "locks"


def ensure_dirs() -> None:
    for d in (CAMPAIGNS, STATE, LOCKS):
        d.mkdir(parents=True, exist_ok=True)


ensure_dirs()


def _windows_retry(fn, attempts: int = 100, delay: float = 0.02):
    """On Windows a file can't be replaced while another thread/process has it open (and a reader can
    briefly get 'access denied' during a replace). Readers hold files for milliseconds, so wait and retry."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(delay)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        _windows_retry(lambda: os.replace(tmp, path))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _read(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_json(path: Path, default=None):
    try:
        return _windows_retry(lambda: _read(path))
    except FileNotFoundError:
        return default


def lock(name: str, timeout: int = 60) -> FileLock:
    return FileLock(str(LOCKS / f"{name}.lock"), timeout=timeout)


# ---------------------------------------------------------------- campaigns
def campaign_dir(cid: str) -> Path:
    return CAMPAIGNS / cid


def campaign_path(cid: str) -> Path:
    return campaign_dir(cid) / "campaign.json"


def load_campaign(cid: str) -> Optional[dict]:
    camp = read_json(campaign_path(cid))
    if camp is not None and "deliveries" not in camp:  # saved by the earlier email version
        camp["deliveries"] = camp.pop("emails", [])
    return camp


def save_campaign(camp: dict) -> None:
    camp["updated_at"] = utcnow().isoformat()
    write_json(campaign_path(camp["campaign_id"]), camp)


@contextmanager
def campaign_tx(cid: str) -> Iterator[dict]:
    """Load -> modify -> save, under the campaign's lock. Nothing is saved if the block raises."""
    with lock(cid):
        camp = load_campaign(cid)
        if camp is None:
            raise KeyError(f"Campaign {cid} not found")
        yield camp
        save_campaign(camp)


def list_campaign_ids() -> list[str]:
    if not CAMPAIGNS.exists():
        return []
    return sorted((p.name for p in CAMPAIGNS.iterdir() if (p / "campaign.json").exists()), reverse=True)


def load_all_campaigns() -> list[dict]:
    out = []
    for cid in list_campaign_ids():
        camp = load_campaign(cid)
        if camp:
            out.append(camp)
    return out


def new_campaign_id() -> str:
    """AMS-YYYYMMDD-NNN (IST date), unique across processes."""
    today = to_local(utcnow()).strftime("%Y%m%d")
    with lock("counter"):
        path = STATE / "counter.json"
        counter = read_json(path, {})
        n = counter.get(today, 0) + 1
        while campaign_dir(f"AMS-{today}-{n:03d}").exists():
            n += 1
        counter = {today: n}
        write_json(path, counter)
    cid = f"AMS-{today}-{n:03d}"
    campaign_dir(cid).mkdir(parents=True, exist_ok=True)
    return cid


# ---------------------------------------------------------------- status / logs
def set_status(key: str, value) -> None:
    with lock("status"):
        path = STATE / "status.json"
        data = read_json(path, {})
        data[key] = value
        write_json(path, data)


def get_status() -> dict:
    return read_json(STATE / "status.json", {})


def append_log(name: str, record: dict) -> None:
    record = {"at": utcnow().isoformat(), **record}
    with lock(f"log-{name}"):
        with open(STATE / f"{name}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_log(name: str, limit: int = 200) -> list[dict]:
    path = STATE / f"{name}.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in reversed(lines) if line.strip()]


__all__ = [n for n in dir() if not n.startswith("_")] + ["LOCAL_TZ"]
