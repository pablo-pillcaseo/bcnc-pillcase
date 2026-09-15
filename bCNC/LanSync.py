"""TEMPORARY stand-in for cross-machine completion sync, over UDP broadcast only.

Until the real endpoint behind EngravingLog exists, stations on the same local
network announce themselves and their completed lids by broadcasting plain UDP
packets - no server, no config, nothing to install. It only works because every
station is on one local, trusted subnet: there is no auth on these packets, by
design, since this is meant to be deleted once the real sync ships.

Protocol (all packets are one UDP datagram of JSON, broadcast to the LAN):
  {"t": "hello", "id": <instance-id>}                     - "I'm here" every 2s
  {"t": "done",  "id": <instance-id>, "k": tote_key, "r": row_key}  - just finished
  {"t": "sync",  "id": <instance-id>, "marks": [[k, r], ...]}       - full catch-up, every 20s

"done" makes peers cross off a lid the instant it happens. "sync" is a periodic
full resend of everything this machine knows, so a station that was off or
missed a packet still converges - "completed" only ever grows, so replaying old
marks is always safe (ProgressStore.mark_many no-ops on what a peer already has).

To remove this feature entirely: delete this file, then in EngravingPage.py
remove the `import LanSync` and the four call sites (`LanSync.start`,
`LanSync.broadcast_completion`, `LanSync.drain`, `LanSync.peer_count`). Nothing
else in the app depends on it.
"""

import json
import queue
import socket
import threading
import time
import uuid

DISCOVERY_PORT = 51837          # arbitrary; first run may prompt Windows Firewall
HELLO_INTERVAL = 2.0
SYNC_INTERVAL = 20.0
PEER_TIMEOUT = 8.0               # ~4 missed hellos before a peer is dropped
MAX_SYNC_BYTES = 60000           # stay well under a UDP datagram's practical limit

_instance_id = uuid.uuid4().hex[:12]
_inbox = queue.Queue()           # (tote_key, row_key) pairs waiting to be merged
_peers = {}                      # instance_id -> last time heard from (monotonic)
_peers_lock = threading.Lock()
_sock = None
_progress = None
_started = False
_start_lock = threading.Lock()


def start(progress_store, machine_name=""):
    """Begin broadcasting/listening. Safe to call more than once - only the
    first call does anything. Never raises: if the port can't be bound (e.g.
    already in use), sync is silently disabled and the app behaves as before.
    """
    global _started, _progress, _sock
    with _start_lock:
        if _started:
            return
        _started = True
    _progress = progress_store
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        print("[lan-sync] disabled - could not open UDP %d: %r" % (DISCOVERY_PORT, e))
        return
    _sock = sock
    threading.Thread(target=_listen, args=(sock,), daemon=True, name="lan-sync-listen").start()
    threading.Thread(target=_hello_beacon, args=(sock,), daemon=True, name="lan-sync-hello").start()
    threading.Thread(target=_sync_beacon, args=(sock,), daemon=True, name="lan-sync-sync").start()


def _send(sock, obj):
    try:
        sock.sendto(json.dumps(obj).encode("utf-8"), ("255.255.255.255", DISCOVERY_PORT))
    except OSError:
        pass  # best-effort - a dropped LAN packet just waits for the next beacon


def _hello_beacon(sock):
    while True:
        _send(sock, {"t": "hello", "id": _instance_id})
        time.sleep(HELLO_INTERVAL)


def _sync_beacon(sock):
    while True:
        time.sleep(SYNC_INTERVAL)
        try:
            marks = _progress.all_marks() if _progress else []
            if not marks:
                continue
            payload = {"t": "sync", "id": _instance_id, "marks": marks}
            if len(json.dumps(payload)) > MAX_SYNC_BYTES:
                continue  # too big for one datagram this cycle - "done" events still flow live
            _send(sock, payload)
        except Exception as e:
            print("[lan-sync] sync beacon error:", repr(e))


def _listen(sock):
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except OSError:
            return
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        peer_id = msg.get("id")
        if not peer_id or peer_id == _instance_id:
            continue  # our own broadcast looping back
        with _peers_lock:
            _peers[peer_id] = time.monotonic()
        mtype = msg.get("t")
        if mtype == "done":
            k, r = msg.get("k"), msg.get("r")
            if k and r:
                _inbox.put((k, r))
        elif mtype == "sync":
            for pair in msg.get("marks") or []:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    _inbox.put((pair[0], pair[1]))


def broadcast_completion(tote_key, row_key):
    """Tell peers a lid just finished, so they cross it off right away."""
    if _sock is not None and tote_key and row_key:
        _send(_sock, {"t": "done", "id": _instance_id, "k": tote_key, "r": row_key})


def peer_count():
    """Other stations heard from within PEER_TIMEOUT. Never counts this one."""
    cutoff = time.monotonic() - PEER_TIMEOUT
    with _peers_lock:
        for pid in [pid for pid, seen in _peers.items() if seen < cutoff]:
            del _peers[pid]
        return len(_peers)


def drain(progress_store):
    """Call once per tick from the Tk main thread. Applies whatever peers sent
    since the last call. Returns True if anything changed.
    """
    pairs = []
    while True:
        try:
            pairs.append(_inbox.get_nowait())
        except queue.Empty:
            break
    return progress_store.mark_many(pairs) if pairs else False
