"""Delivery of completed-engraving records. Currently a stub: nothing is sent.

`submit(record)` is the only entry point and is what the engraving tab calls when
a lid finishes. It hands the record to a daemon thread and returns at once, and
the thread swallows every error - so whatever `_deliver` is made to do later, it
cannot slow down, block or break the engraving workflow.

To turn sending on:
  1. Implement `_deliver` (e.g. POST the record as JSON to the logger's per-
     engraving endpoint, with a timeout and a bearer token from keyring).
  2. Set SEND_ENABLED = True.
The record's shape is defined by EngravingSession.completion_record; `event_id`
is a fresh UUID per record so the receiver can make resends idempotent.
"""

import json
import queue
import threading

SEND_ENABLED = False

_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def submit(record):
    """Queue a record for delivery. Never blocks, never raises."""
    try:
        _ensure_worker()
        _queue.put_nowait(record)
    except Exception as e:
        print("[engraving-log] could not queue record:", repr(e))


def _deliver(record):
    """STUB - send one record. Replace the body when the endpoint exists.

    Return True when the record was accepted.
    """
    return True


def _run():
    while True:
        record = _queue.get()
        try:
            if SEND_ENABLED:
                ok = _deliver(record)
                print("[engraving-log] sent" if ok else "[engraving-log] send failed",
                      record.get("event_id"))
            else:
                print("[engraving-log] sending disabled - record not sent:",
                      json.dumps(record))
        except Exception as e:
            print("[engraving-log] delivery error:", repr(e))
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="engraving-log", daemon=True)
            _worker.start()
