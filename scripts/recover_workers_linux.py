#!/usr/bin/env python3
"""Bounded Linux boot recovery. No Docker, DB, campaign or authorization writes.

Run after the Compose start-only unit. Only native /workers/{id}/recover may
change state. The API rechecks durable intent under its manual-stop lock.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
from urllib import request
from urllib.parse import urlsplit


DEFAULT_EXCLUDED = frozenset({22, 71})
MAX_BODY = 1024 * 1024


class SnapshotInvalid(ValueError):
    pass


def log_event(event, **fields):
    # Callers supply fixed event names and safe scalar/count/ID fields only.
    print(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                      "event": event, **fields}, sort_keys=True), flush=True)


def loopback_url(value):
    """Credentials, remote hosts, paths, redirects and environment proxies forbidden."""
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.path in ("", "/")
                 and ipaddress.ip_address(parsed.hostname).is_loopback
                 and 0 < (parsed.port or 80) < 65536)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("API must be an HTTP loopback address without credentials or path")
    return value.rstrip("/")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalAPI:
    def __init__(self, base_url):
        self.base_url = loopback_url(base_url)
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def __call__(self, method, path, timeout):
        req = request.Request(self.base_url + path, method=method,
                              headers={"Accept": "application/json"},
                              data=b"" if method == "POST" else None)
        with self.opener.open(req, timeout=timeout) as response:
            body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise SnapshotInvalid("response too large")
        return json.loads(body)


def workers_from_snapshot(snapshot):
    """Reject partial/ambiguous snapshots instead of treating missing fields as false."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("workers"), list):
        raise SnapshotInvalid("workers missing")
    workers = snapshot["workers"]
    seen = set()
    for worker in workers:
        if not isinstance(worker, dict):
            raise SnapshotInvalid("worker invalid")
        account_id = worker.get("account_id")
        if type(account_id) is not int or account_id <= 0 or account_id in seen:
            raise SnapshotInvalid("account ID invalid")
        seen.add(account_id)
        for field in ("desired_running", "is_active", "recoverable", "task_alive", "connected"):
            if type(worker.get(field)) is not bool:
                raise SnapshotInvalid("worker flag invalid")
        if not isinstance(worker.get("status"), str):
            raise SnapshotInvalid("worker status missing")
        if worker["connected"] and not worker["task_alive"]:
            raise SnapshotInvalid("connected without task")
    desired = sum(w["desired_running"] for w in workers)
    if type(snapshot.get("desired")) is not int or snapshot["desired"] != desired:
        raise SnapshotInvalid("desired count mismatch")
    return workers


def recovery_candidates(workers, excluded=DEFAULT_EXCLUDED):
    return sorted(w["account_id"] for w in workers
                  if w["account_id"] not in excluded and w["desired_running"]
                  and w["is_active"] and w["recoverable"] and not w["task_alive"]
                  and not w["connected"] and w["status"] != "session_expired")


def recover(api, *, timeout=600, poll_seconds=5, excluded=DEFAULT_EXCLUDED,
            maintenance=lambda: False, emit=log_event, clock=time.monotonic,
            sleep=time.sleep):
    """One request per ID/run, even on timeout/uncertain response. No silent retry."""
    deadline = clock() + timeout
    requested = set()
    last_not_ready = []
    emit("started", timeout_seconds=timeout, excluded_ids=sorted(excluded))

    def call(method, path):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError()
        return api(method, path, min(10, remaining))

    while clock() < deadline:
        if maintenance():
            emit("maintenance_skipped")
            return 0
        try:
            health = call("GET", "/system/health")
            if not isinstance(health, dict) or health.get("status") != "ok":
                raise SnapshotInvalid("health not ready")
            workers = workers_from_snapshot(call("GET", "/system/readiness"))
            desired = [w for w in workers if w["desired_running"]]
            last_not_ready = [w["account_id"] for w in desired
                              if not (w["task_alive"] and w["connected"])]
            if not last_not_ready:
                emit("ready", desired=len(desired), connected=len(desired))
                return 0
            for account_id in recovery_candidates(workers, excluded):
                if maintenance():
                    emit("maintenance_skipped")
                    return 0
                if clock() >= deadline:
                    break
                if account_id in requested:
                    continue
                requested.add(account_id)
                try:
                    call("POST", f"/workers/{account_id}/recover")
                    emit("worker_requested", account_id=account_id)
                except Exception:
                    # Never log exception text/body/headers: those may contain secrets.
                    emit("worker_request_failed", account_id=account_id)
        except Exception:
            emit("api_wait")
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(poll_seconds, remaining))
    emit("timeout", requested_ids=sorted(requested), not_ready_ids=last_not_ready)
    return 1


@contextmanager
def singleton(path):
    """The shared flock also covers manual invocations. Never unlink its inode."""
    import fcntl  # Linux only; pure guard tests can still run on Windows.
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def bounded_timeout(value):
    parsed = int(value)
    if not 30 <= parsed <= 1800:
        raise argparse.ArgumentTypeError("timeout must be 30..1800 seconds")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-path", type=Path, default=Path("/opt/projectoutrich"))
    parser.add_argument("--api-url", type=loopback_url, default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=bounded_timeout, default=600)
    parser.add_argument("--lock-path", type=Path,
                        default=Path("/run/projectoutrich-worker-recovery/recovery.lock"))
    parser.add_argument("--exclude-id", action="append", type=int, default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    # Deployment safety floor is intentionally not removable through CLI.
    excluded = DEFAULT_EXCLUDED | frozenset(args.exclude_id)
    if args.dry_run:
        log_event("dry_run", timeout_seconds=args.timeout_seconds,
                  excluded_ids=sorted(excluded), actions=["wait health/readiness", "native desired worker recovery"])
        return 0
    try:
        if not (args.project_path / "docker-compose.vps.yml").is_file():
            log_event("configuration_invalid")
            return 2
        maintenance = lambda: (args.project_path / ".maintenance").exists()
        if maintenance():
            log_event("maintenance_skipped")
            return 0
        with singleton(args.lock_path) as locked:
            if not locked:
                log_event("already_running")
                return 0
            return recover(LocalAPI(args.api_url), timeout=args.timeout_seconds,
                           excluded=excluded, maintenance=maintenance)
    except Exception:
        log_event("recovery_failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
