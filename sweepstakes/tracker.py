"""
Entry logging and tracking.

Maintains a JSON log with rich metadata, duplicate detection,
statistics, and export capabilities.
"""

import json
import logging
from datetime import datetime
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SweepstakesEntry:
    """Record of a single sweepstakes entry."""

    name: str
    url: str
    sponsor: str
    prize_description: str
    entry_date: str
    end_date: str
    status: str            # entered, skipped, failed, scam_detected
    source_site: str
    validation_confidence: float
    notes: str = ""
    entry_method: str = "online_form"
    estimated_value: str = ""
    entry_frequency: str = "one_time"

    def to_dict(self) -> dict:
        return asdict(self)


class EntryTracker:
    """Track sweepstakes entries — dedup, stats, history."""

    def __init__(self, log_path: str = "sweepstakes_entries.json"):
        self.log_path = Path(log_path)
        self.entries: list[dict] = []
        self._load()

    def _load(self):
        if self.log_path.exists():
            try:
                with open(self.log_path) as f:
                    data = json.load(f)
                    self.entries = data.get("entries", [])
                    logger.info(f"Loaded {len(self.entries)} entries from {self.log_path}")
            except (json.JSONDecodeError, KeyError):
                logger.warning(f"Could not parse {self.log_path}, starting fresh")
                self.entries = []

    def _save(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        stats = self.get_stats()
        data = {
            "last_updated": datetime.now().isoformat(),
            **stats,
            "entries": self.entries,
        }
        with open(self.log_path, "w") as f:
            json.dump(data, f, indent=2)

    def has_entered(self, url: str) -> bool:
        normalized = url.rstrip("/").lower()
        return any(
            e.get("url", "").rstrip("/").lower() == normalized
            and e.get("status") == "entered"
            for e in self.entries
        )

    def add_entry(self, entry: SweepstakesEntry):
        if self.has_entered(entry.url):
            logger.info(f"Already entered: {entry.name}")
            return
        self.entries.append(entry.to_dict())
        self._save()
        logger.info(f"Recorded: {entry.name} ({entry.status})")

    def skip_entry(self, name: str, url: str, reason: str):
        self.entries.append({
            "name": name,
            "url": url,
            "status": "skipped",
            "entry_date": datetime.now().isoformat(),
            "notes": reason,
        })
        self._save()

    def get_stats(self) -> dict:
        total = len(self.entries)
        entered = sum(1 for e in self.entries if e.get("status") == "entered")
        skipped = sum(1 for e in self.entries if e.get("status") == "skipped")
        failed = sum(1 for e in self.entries if e.get("status") == "failed")
        scams = sum(1 for e in self.entries if e.get("status") == "scam_detected")

        # Value tracking
        total_value = 0
        for e in self.entries:
            if e.get("status") == "entered" and e.get("estimated_value"):
                val = e["estimated_value"].replace("$", "").replace(",", "").strip()
                try:
                    total_value += float(val)
                except ValueError:
                    pass

        return {
            "total_entries": total,
            "total_processed": total,
            "entered": entered,
            "successfully_entered": entered,
            "skipped": skipped,
            "failed": failed,
            "scams_detected": scams,
            "total_prize_value": f"${total_value:,.0f}" if total_value else "$0",
            "success_rate": f"{entered / total * 100:.0f}%" if total else "N/A",
        }

    def reload(self):
        """Reload entries from disk."""
        self._load()

    def get_entered_urls(self) -> set[str]:
        return {
            e.get("url", "").rstrip("/").lower()
            for e in self.entries
            if e.get("status") == "entered"
        }

    def get_recent(self, n: int = 50) -> list[dict]:
        """Most recent entries, newest first."""
        return list(reversed(self.entries[-n:]))

    def print_summary(self):
        stats = self.get_stats()
        print("\n" + "=" * 60)
        print("  SWEEPSTAKES ENTRY SUMMARY")
        print("=" * 60)
        print(f"  Total processed:     {stats['total_processed']}")
        print(f"  Successfully entered: {stats['successfully_entered']}")
        print(f"  Skipped:             {stats['skipped']}")
        print(f"  Failed:              {stats['failed']}")
        print(f"  Scams blocked:       {stats['scams_detected']}")
        print(f"  Total prize value:   {stats['total_prize_value']}")
        print(f"  Success rate:        {stats['success_rate']}")
        print("=" * 60)

        entered = [e for e in self.entries if e.get("status") == "entered"]
        if entered:
            print("\n  Recent entries:")
            for e in entered[-10:]:
                print(f"    - {e.get('name', 'Unknown')}")
                print(f"      Prize: {e.get('prize_description', 'N/A')}")
        print()
