"""Engraver login, tote progress and the completed-engraving record.

Mirrors the engraving-station-logger's station flow so an engraver signs in at the
machine the same way they do at the tablet: tap a roster name or scan/type a badge,
the session counts what they finish, and an idle station logs itself out.

Nothing here touches Tk, so the rules can be tested on their own. EngravingPage is
the UI over it; EngravingLog is where a finished record is sent.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

# The logger's default roster, so an unconfigured machine behaves like an
# unconfigured tablet. The idle logout is a full day: an engraver stays logged in
# across breaks and quiet spells, and is logged out only once the machine has sat
# unused for 24 hours.
DEFAULT_ENGRAVERS = "Halil Gurler,Manu Bekele,Maurice Williams"
DEFAULT_IDLE_MINUTES = 24 * 60

# This application cuts with a spindle. A separate laser application will report
# "Laser" into the same log, so the method travels with every record.
METHOD = "CNC"

# Progress older than this is dropped, so the file never grows without bound. A
# tote is engraved within a shift; two weeks is ample for a rescan to find it.
PROGRESS_KEEP_DAYS = 14


def now_utc():
    return datetime.now(timezone.utc)


def iso(ts):
    return ts.isoformat(timespec="seconds") if ts else None


# ------------------------------------------------------------------ login
def parse_roster(text):
    """'A, B,,C' -> ['A', 'B', 'C']."""
    return [n.strip() for n in str(text or "").split(",") if n.strip()]


def valid_engraver_name(name):
    """A real name has letters; a tote barcode scanned into the login is digits.

    Same rule as the logger: at least two letters. Roster membership is not
    required, so a new hire typing their name is never locked out.
    """
    return sum(ch.isalpha() for ch in (name or "")) >= 2


class Session:
    """Who is engraving at this machine, since when, and how much they finished."""

    def __init__(self):
        self.engraver = None
        self.since = None
        self.last_active = None
        self.count = 0

    @property
    def logged_in(self):
        return bool(self.engraver)

    def login(self, name, now=None):
        now = now or now_utc()
        self.engraver = name.strip()
        self.since = now
        self.last_active = now
        self.count = 0          # per login, as at the tablet

    def logout(self):
        self.engraver = None
        self.since = None
        self.last_active = None
        self.count = 0

    def touch(self, now=None):
        if self.logged_in:
            self.last_active = now or now_utc()

    def is_idle(self, idle_seconds, now=None):
        if not self.logged_in or self.last_active is None:
            return False
        return ((now or now_utc()) - self.last_active).total_seconds() > idle_seconds

    def rate_per_hour(self, now=None):
        """Engravings per hour this login, or None until there is enough to say."""
        if not self.logged_in or not self.count:
            return None
        hours = ((now or now_utc()) - self.since).total_seconds() / 3600.0
        return self.count / hours if hours > 0.01 else None


# --------------------------------------------------------------- the rows
def has_engraving(row):
    text = str((row or {}).get("engraving") or "").strip()
    return bool(text) and text != "None"


def row_keys(rows):
    """A stable identity per row, so a rescan of the same tote finds its progress.

    Rows carry no id of their own. Order, SKU and text name a lid; the occurrence
    count separates two identical lids in one order (same SKU, same text), which
    are two real engravings and must be crossed off one at a time.
    """
    seen = {}
    keys = []
    for row in rows:
        base = "|".join((
            str(row.get("order_number") or ""),
            str(row.get("sku") or ""),
            str(row.get("engraving") or ""),
        ))
        n = seen.get(base, 0)
        seen[base] = n + 1
        keys.append("%s|%d" % (base, n))
    return keys


def normalise_scan(value):
    """What the operator scanned, reduced to what identifies the tote or order."""
    return str(value or "").strip().lstrip("#").strip().upper()


def scan_key(search_mode, value):
    return "%s:%s" % (search_mode or "Order", normalise_scan(value))


# ---------------------------------------------------------------- progress
class ProgressStore:
    """Which engravings of each scanned tote/order are done, kept on disk.

    On disk so a rescan, a restart or a crash mid-tote never offers a finished lid
    for engraving again. Every failure to read or write is swallowed: losing the
    crossed-off marks is an inconvenience, stopping the machine over it is not.
    """

    def __init__(self, path):
        self.path = path
        self._data = self._read()

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write(self):
        self._prune()
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            print("[engraving] could not save progress:", repr(e))

    def _prune(self, now=None):
        cutoff = (now or now_utc()) - timedelta(days=PROGRESS_KEEP_DAYS)
        for key in list(self._data):
            try:
                updated = datetime.fromisoformat(self._data[key].get("updated"))
            except Exception:
                updated = None
            if updated is None or updated < cutoff:
                del self._data[key]

    def completed(self, key):
        entry = self._data.get(key) or {}
        return set(entry.get("completed") or [])

    def mark(self, key, row_key, now=None):
        entry = self._data.setdefault(key, {"completed": []})
        if row_key not in entry["completed"]:
            entry["completed"].append(row_key)
        entry["updated"] = iso(now or now_utc())
        self._write()


# ------------------------------------------------------------------ record
def completion_record(engraver, engraving_text, machine, lid, row, job,
                      started_at, completed_at):
    """The log entry for one lid that finished engraving without error.

    `engraving_text` is what was in the Engrave Text field when the G-code was
    generated - the text actually cut - not the order's text, so a typo the
    operator fixed by hand is logged as it was engraved. `row` and `job` are None
    when the engraving was set up by hand rather than picked from a scanned tote.
    """
    row = row or {}
    job = job or {}
    duration = None
    if started_at and completed_at:
        duration = round((completed_at - started_at).total_seconds(), 1)
    return {
        "event": "engraving_complete",
        "event_id": str(uuid.uuid4()),       # lets a receiver ignore a resend
        "engraver": engraver or None,
        "engraving_text": engraving_text,
        "method": METHOD,
        "machine": machine or None,
        "case": {
            "lid": lid or None,
            "case_type": row.get("case_type") or None,
            "product_code": row.get("product_code") or None,
            "colour_code": row.get("colour_code") or None,
            "colour_name": row.get("colour_name") or None,
            "sku": row.get("sku") or None,
        },
        "order_number": row.get("order_number") or None,
        "scan": {
            "value": job.get("scan") or None,
            "search_mode": job.get("search_mode") or None,
        },
        "started_at": iso(started_at),
        "completed_at": iso(completed_at),
        "duration_seconds": duration,
    }
