"""Isolated recovery contract tests; no Docker, production HTTP, DB or Telegram."""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("linux_recovery", ROOT / "scripts/recover_workers_linux.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def worker(account_id=23, **changes):
    row = dict(account_id=account_id, desired_running=True, is_active=True,
               recoverable=True, task_alive=False, connected=False, status="stopped")
    row.update(changes)
    return row


def snapshot(*rows):
    return dict(workers=list(rows), desired=sum(w["desired_running"] is True for w in rows))


class FakeClock:
    now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RecoveryTests(unittest.TestCase):
    def run_recovery(self, api, **kwargs):
        self.clock = FakeClock()
        self.events = []
        return recovery.recover(api, timeout=12, poll_seconds=2,
                                clock=self.clock, sleep=self.clock.sleep,
                                emit=lambda event, **fields: self.events.append({"event": event, **fields}),
                                **kwargs)

    def test_only_desired_active_unstarted_nonexpired_nonexcluded(self):
        rows = [worker(), worker(22), worker(71),
                worker(24, desired_running=False), worker(25, is_active=False),
                worker(26, recoverable=False), worker(27, task_alive=True),
                worker(28, status="session_expired"),
                worker(29, task_alive=True, connected=True)]
        self.assertEqual([23], recovery.recovery_candidates(rows))

    def test_restore_64_then_fresh_actual_readiness(self):
        rows = [worker(i) for i in range(23, 88) if i != 71]
        rows += [worker(71, desired_running=False, status="session_expired")]
        pending = set()
        calls = []
        def api(method, path, timeout):
            calls.append((method, path))
            if path == "/system/health":
                return {"status": "ok"}
            if method == "POST":
                pending.add(int(path.split("/")[2]))
                return {}
            for row in rows:
                if row["account_id"] in pending:
                    row.update(task_alive=True, connected=True)
            return snapshot(*[dict(r) for r in rows])
        self.assertEqual(0, self.run_recovery(api))
        self.assertEqual(64, len(pending))
        self.assertNotIn(71, pending)
        self.assertEqual(2, calls.count(("GET", "/system/readiness")))
        self.assertEqual({"event": "ready", "desired": 64, "connected": 64}, self.events[-1])

    def test_uncertain_post_is_not_retried_or_logged(self):
        posts = []
        def api(method, path, timeout):
            if method == "POST":
                posts.append(path)
                raise RuntimeError("SECRET_TOKEN_AND_PROXY_PASSWORD")
            return {"status": "ok"} if path.endswith("health") else snapshot(worker())
        self.assertEqual(1, self.run_recovery(api))
        self.assertEqual(["/workers/23/recover"], posts)
        self.assertNotIn("SECRET", json.dumps(self.events))

    def test_connected_and_alive_required_not_cached_running(self):
        api = MagicMock(side_effect=lambda method, path, timeout:
                        {"status": "ok"} if path.endswith("health") else
                        snapshot(worker(task_alive=True, status="running")))
        self.assertEqual(1, self.run_recovery(api))
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))
        self.assertEqual([23], self.events[-1]["not_ready_ids"])

    def test_already_live_has_no_mutation(self):
        api = MagicMock(side_effect=lambda method, path, timeout:
                        {"status": "ok"} if path.endswith("health") else
                        snapshot(worker(task_alive=True, connected=True)))
        self.assertEqual(0, self.run_recovery(api))
        self.assertEqual(2, api.call_count)
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_health_failure_never_requests_recovery(self):
        api = MagicMock(return_value={"status": "degraded"})
        self.assertEqual(1, self.run_recovery(api))
        self.assertTrue(all(call.args[1] == "/system/health" for call in api.call_args_list))

    def test_manual_stop_after_snapshot_remains_off(self):
        state = worker()
        posts = []
        def api(method, path, timeout):
            if method == "POST":
                # Native manager observes newer operator stop and refuses recover.
                state.update(desired_running=False, recoverable=False)
                posts.append(path)
                raise RuntimeError("RECOVERY_DISABLED")
            return {"status": "ok"} if path.endswith("health") else snapshot(dict(state))
        self.assertEqual(0, self.run_recovery(api))
        self.assertEqual(["/workers/23/recover"], posts)
        self.assertEqual(0, self.events[-1]["desired"])

    def test_maintenance_between_workers_stops_further_requests(self):
        posts = []
        def api(method, path, timeout):
            if method == "POST":
                posts.append(path)
                return {}
            return {"status": "ok"} if path.endswith("health") else snapshot(worker(), worker(24))
        self.assertEqual(0, self.run_recovery(api, maintenance=lambda: bool(posts)))
        self.assertEqual(["/workers/23/recover"], posts)
        self.assertEqual("maintenance_skipped", self.events[-1]["event"])

    def test_deadline_caps_each_request_and_sleep(self):
        timeouts = []
        def api(method, path, timeout):
            timeouts.append(timeout)
            self.clock.sleep(timeout)
            raise TimeoutError()
        self.assertEqual(1, self.run_recovery(api))
        self.assertEqual(12, self.clock.now)
        self.assertEqual([10], timeouts)

    def test_blocked_desired_account_never_falsely_ready(self):
        posts = []
        def api(method, path, timeout):
            if method == "POST":
                posts.append(path)
            return {"status": "ok"} if path.endswith("health") else snapshot(worker(71))
        self.assertEqual(1, self.run_recovery(api))
        self.assertEqual([], posts)
        self.assertEqual([71], self.events[-1]["not_ready_ids"])

    def test_invalid_snapshot_fails_closed(self):
        invalid = [None, {"workers": []}, snapshot(worker(desired_running="true")),
                   snapshot(worker(), worker()), snapshot(worker(account_id=True)),
                   snapshot(worker(connected=True)),
                   {"workers": [worker()], "desired": 2}]
        for state in invalid:
            with self.subTest(state=state):
                with self.assertRaises(recovery.SnapshotInvalid):
                    recovery.workers_from_snapshot(state)

    def test_dry_run_has_no_http_lock_or_filesystem_access(self):
        with patch.object(recovery, "LocalAPI") as api, patch.object(recovery, "singleton") as lock:
            with patch.object(Path, "is_file", side_effect=AssertionError("filesystem")):
                with redirect_stdout(io.StringIO()) as output:
                    result = recovery.main(["--dry-run", "--exclude-id", "23"])
        self.assertEqual(0, result)
        api.assert_not_called()
        lock.assert_not_called()
        self.assertEqual([22, 23, 71], json.loads(output.getvalue())["excluded_ids"])

    def test_loopback_only_no_credentials_or_redirects(self):
        for url in ("https://127.0.0.1", "http://88.216.73.68", "http://user:secret@127.0.0.1",
                    "http://127.0.0.1/path", "http://127.0.0.1?secret=x", "http://localhost"):
            with self.subTest(url=url), self.assertRaises(argparse.ArgumentTypeError):
                recovery.loopback_url(url)
        self.assertEqual("http://127.0.0.1:8000", recovery.loopback_url("http://127.0.0.1:8000/"))
        self.assertIsNone(recovery.NoRedirect().redirect_request(None, None, 302, "", {}, "http://remote"))

    def test_environment_proxy_bypassed_and_response_bounded(self):
        with patch.object(recovery.request, "build_opener") as build:
            api = recovery.LocalAPI("http://127.0.0.1:8000")
            self.assertEqual({}, build.call_args.args[0].proxies)
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b"x" * (recovery.MAX_BODY + 1)
            with self.assertRaises(recovery.SnapshotInvalid):
                api("GET", "/system/readiness", 1)

    @unittest.skipIf(os.name != "posix", "flock is Linux/Unix only")
    def test_real_flock_ignores_duplicate_and_can_reacquire(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "lock"
            with recovery.singleton(lock) as first:
                self.assertTrue(first)
                with recovery.singleton(lock) as second:
                    self.assertFalse(second)
            with recovery.singleton(lock) as third:
                self.assertTrue(third)

    @unittest.skipIf(os.name != "posix", "O_NOFOLLOW is Linux/Unix only")
    def test_lock_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "target"
            target.write_text("preserve")
            link = Path(folder) / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError), recovery.singleton(link):
                pass
            self.assertEqual("preserve", target.read_text())

    def test_units_are_start_only_single_app_and_unprivileged_helper(self):
        compose = (ROOT / "deploy/systemd/projectoutrich-compose.service").read_text()
        workers = (ROOT / "deploy/systemd/projectoutrich-workers.service").read_text()
        for unit in (compose, workers):
            self.assertIn("ConditionPathExists=!/opt/projectoutrich/.maintenance", unit)
            self.assertNotIn("\nExecStop=", unit)
            self.assertNotIn("\nRestart=", unit)
        self.assertIn("-p projectoutrich -f docker-compose.vps.yml start", compose)
        self.assertIn("User=projectoutrich", workers)
        self.assertIn("Requires=projectoutrich-compose.service", workers)
        self.assertIn("RuntimeDirectoryPreserve=yes", workers)
        self.assertNotIn("docker.sock", workers)
        self.assertNotIn("RemainAfterExit=yes", workers)


if __name__ == "__main__":
    unittest.main()
