#!/usr/bin/env python3
"""Black-box acceptance tests for s3backup, without a router or cloud account.

The program runs unchanged against temporary directories. Only the S3 transport
and the mount/UUID inventory are doubles. On Linux, hashing, flock, /proc,
process signalling, JSON parsing and filesystem operations are real.
"""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
NOW = int(dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc).timestamp())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class BackupAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s3backup-acceptance-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.prefix = self.base / "entware"
        self.hdd = self.base / "hdd"
        self.backups = self.hdd / "tkbackup"
        self.s3 = self.base / "s3"
        self.remote = self.s3 / "testbucket" / "backups"
        self.configdir = self.prefix / "etc" / "s3backup"
        self.statedir = self.prefix / "var" / "lib" / "s3backup"
        self.rundir = self.prefix / "var" / "run" / "s3backup"
        self.callsfile = self.base / "rclone-calls.jsonl"
        self.controlfile = self.base / "rclone-control.json"
        self.mounts = self.base / "mounts"
        self.uuids = self.base / "uuids.json"
        for path in (self.prefix / "bin", self.prefix / "sbin", self.backups, self.remote, self.configdir, self.statedir, self.rundir):
            path.mkdir(parents=True, exist_ok=True)
        self.backups.joinpath(".tkbackup-owner").write_text("tkbackup-v1\n")
        self.mounts.write_text(f"/dev/entware {self.prefix} ext4 rw 0 0\n/dev/hdd {self.hdd} ext4 rw 0 0\n")
        write_json(self.uuids, {"/dev/entware": "entware-uuid", "/dev/hdd": "hdd-uuid"})
        write_json(self.controlfile, {})
        self.config = {
            "version": 1, "endpoint": "https://s3.example.invalid", "region": "ru-central-1",
            "bucket": "testbucket", "prefix": "backups", "path_style": True,
            "backup_dir": str(self.backups), "mount_point": str(self.hdd), "mount_uuid": "hdd-uuid",
            "time": "05:00", "timezone": "MSK-3", "interval_days": 1,
            "retention_days": 30, "bandwidth_mib": 0, "reserve_mib": 1,
        }
        self.save_config()
        self.secret = "FAKE_SECRET_MUST_NOT_APPEAR_IN_LOGS_73"
        self.configdir.joinpath("rclone.conf").write_text(
            "[s3backup]\ntype = s3\nprovider = Other\n"
            "endpoint = https://s3.example.invalid\nregion = ru-central-1\n"
            "access_key_id = FAKE_ACCESS_KEY\nsecret_access_key = " + self.secret + "\n"
            "force_path_style = true\nno_check_bucket = true\n"
        )
        self.configdir.joinpath("rclone.conf").chmod(0o600)
        self.configdir.chmod(0o700)
        self.make_executable("rclone", f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(HERE / 'fake_rclone.py')!r}, run_name='__main__')\n")
        self.make_executable("blkid", f"#!{sys.executable}\nimport json,os,sys\nvalues=json.load(open(os.environ['FAKE_UUIDS']))\nvalue=values.get(sys.argv[-1])\nif value: print(value)\nsys.exit(0 if value else 2)\n")
        # Installation/start tests use a harmless cron init script; no host service.
        init = self.prefix / "etc" / "init.d"
        init.mkdir(parents=True, exist_ok=True)
        init.joinpath("S10cron").write_text("#!/bin/sh\nexit 0\n")
        init.joinpath("S10cron").chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            "S3BACKUP_PREFIX": str(self.prefix), "S3BACKUP_MOUNTS_FILE": str(self.mounts),
            "S3BACKUP_NOW": str(NOW), "FAKE_S3_ROOT": str(self.s3),
            "FAKE_RCLONE_CALLS": str(self.callsfile), "FAKE_RCLONE_CONTROL": str(self.controlfile),
            "FAKE_UUIDS": str(self.uuids), "PATH": str(self.prefix / "bin") + os.pathsep + os.environ["PATH"],
        })
        self.cli = PROJECT / "s3backup"

    def make_executable(self, name, text):
        path = self.prefix / "bin" / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def save_config(self):
        write_json(self.configdir / "config.json", self.config)
        (self.configdir / "config.json").chmod(0o600)

    def interactive(self, argv, answers, timeout=15):
        """Use a controlling terminal: the real wizard reads /dev/tty."""
        pid, terminal = pty.fork()
        if pid == 0:
            os.execvpe(argv[0], argv, self.env)
        output = b""
        cursor = 0
        reaped = False
        deadline = time.monotonic() + timeout
        try:
            for prompt, answer in answers:
                while prompt.encode() not in output[cursor:]:
                    self.assertLess(time.monotonic(), deadline, f"Prompt not seen: {prompt}\n{output.decode(errors='replace')}")
                    ready, _, _ = select.select([terminal], [], [], 0.05)
                    if ready:
                        try:
                            data = os.read(terminal, 65536)
                        except OSError:
                            data = b""
                        self.assertTrue(data, f"Wizard exited before prompt: {prompt}\n{output.decode(errors='replace')}")
                        output += data
                cursor = len(output)
                # Let read -s disable the terminal's echo before feeding secret text.
                time.sleep(0.02)
                os.write(terminal, answer.encode() + b"\n")
            while time.monotonic() < deadline:
                ready, _, _ = select.select([terminal], [], [], 0.05)
                if ready:
                    try:
                        data = os.read(terminal, 65536)
                    except OSError:
                        data = b""
                    if data:
                        output += data
                ended, status = os.waitpid(pid, os.WNOHANG)
                if ended:
                    reaped = True
                    return os.waitstatus_to_exitcode(status), output.decode(errors="replace")
            self.fail("Interactive command did not finish: " + output.decode(errors="replace"))
        finally:
            os.close(terminal)
            if not reaped:
                try:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
                except ProcessLookupError:
                    pass

    def start_slow_worker(self):
        self.snapshot()
        write_json(self.controlfile, {"archive_delay_seconds": 20})
        worker = subprocess.Popen(["bash", str(self.cli), "run"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_worker, worker)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            archives = [c for c in self.calls("copyto") if c["source"].endswith(".tar.zst")]
            if archives:
                return worker, archives[-1]["pid"]
            self.assertIsNone(worker.poll(), "worker exited before archive transfer")
            time.sleep(0.05)
        self.fail("worker never began archive transfer")

    @staticmethod
    def cleanup_worker(worker):
        if worker.poll() is None:
            worker.terminate()
        try:
            worker.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate(timeout=5)

    def invoke(self, *args, ok=None, env=None, timeout=30, input=None):
        result = subprocess.run(["bash", str(self.cli), *args], env=env or self.env, capture_output=True, text=True, input=input, timeout=timeout)
        if ok is True:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if ok is False:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(self.secret, result.stdout + result.stderr)
        return result

    def calls(self, command=None):
        values = [json.loads(line) for line in self.callsfile.read_text().splitlines()] if self.callsfile.exists() else []
        self.assertNotIn(self.secret, json.dumps(values))
        return [call for call in values if command is None or call["command"] == command]

    def snapshot(self, name="snapshot-01", age_days=0, payload=None, mutate=None, sidecar=True, legacy=False):
        payload = payload if payload is not None else (b"opaque-compressed-archive:" + name.encode()) * 23
        filename = name + ".tar.zst"
        stamp = dt.datetime.fromtimestamp(NOW - age_days * 86400, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        manifest = {
            "snapshotId": name, "appVersion": "5684066", "snapshotAt": stamp,
            "objectKey": "backups/" + filename, "bucket": "testbucket", "sizeBytes": len(payload),
            "sha256Ciphertext": hashlib.sha256(payload).hexdigest(), "partCount": 1,
        }
        if mutate:
            manifest.update(mutate)
        self.remote.joinpath(filename).write_bytes(payload)
        if sidecar:
            write_json(self.remote / ((name if legacy else filename) + ".manifest.json"), manifest)
        return manifest, payload

    def local_snapshot(self, name, age_days, payload=None):
        manifest, payload = self.snapshot(name, age_days, payload)
        local = self.backups / "snapshots" / name
        local.mkdir(parents=True, exist_ok=True)
        filename = name + ".tar.zst"
        local.joinpath(filename).write_bytes(payload)
        write_json(local / (filename + ".manifest.json"), manifest)
        write_json(local / ".verified.json", manifest)
        self.remote.joinpath(filename).unlink()
        self.remote.joinpath(filename + ".manifest.json").unlink()
        return local

    def test_accepts_pair_and_rechecks_without_archive_redownload(self):
        manifest, payload = self.snapshot()
        self.invoke("run", ok=True)
        dest = self.backups / "snapshots" / manifest["snapshotId"]
        self.assertEqual((dest / "snapshot-01.tar.zst").read_bytes(), payload)
        self.assertEqual(json.loads((dest / ".verified.json").read_text())["sha256Ciphertext"], manifest["sha256Ciphertext"])
        archives_before = len([x for x in self.calls("copyto") if x["source"].endswith(".tar.zst")])
        self.invoke("run", ok=True)
        archives_after = len([x for x in self.calls("copyto") if x["source"].endswith(".tar.zst")])
        self.assertEqual(archives_before, 1)
        self.assertEqual(archives_after, archives_before)
        self.assertTrue(all(call["source"].startswith("s3backup:") for call in self.calls()))

    @unittest.skipUnless(os.environ.get("S3BACKUP_REQUIRE_NO_ONIG") == "1", "enable when running against a jq built without Oniguruma")
    def test_entware_jq_build_really_has_no_regex_engine(self):
        # This deliberately calls a regex builtin to prove that the full suite
        # is exercising the same missing capability as the router's jq build.
        result = subprocess.run(["jq", "-n", '"probe" | test("probe")'], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ONIGURUMA", result.stderr.upper())

    def test_legacy_sidecar_is_accepted(self):
        self.snapshot("legacy_01", legacy=True)
        self.invoke("run", ok=True)
        self.assertTrue((self.backups / "snapshots" / "legacy_01" / ".verified.json").exists())

    def test_archive_without_manifest_is_skipped_while_valid_pair_and_retention_succeed(self):
        old = self.local_snapshot("old-01", 45)
        self.snapshot("orphan-01", sidecar=False)
        manifest, payload = self.snapshot("valid-01")
        self.invoke("run", ok=True)
        dest = self.backups / "snapshots" / "valid-01"
        self.assertEqual((dest / "valid-01.tar.zst").read_bytes(), payload)
        self.assertEqual(json.loads((dest / ".verified.json").read_text()), manifest)
        self.assertFalse(old.exists())
        self.assertFalse((self.backups / "snapshots" / "orphan-01").exists())
        self.assertFalse(any(call["source"].endswith("/orphan-01.tar.zst") for call in self.calls("copyto")))
        self.assertEqual(json.loads((self.statedir / "result.json").read_text())["result"], "success")

    def test_only_orphan_archives_skip_without_pruning_or_claiming_copy(self):
        old = self.local_snapshot("old-01", 45)
        recent = self.local_snapshot("recent-01", 1)
        latest = {"snapshotId": "recent-01", "snapshotAt": NOW - 86400}
        write_json(self.statedir / "latest.json", latest)
        self.snapshot("orphan-01", sidecar=False)
        result = self.invoke("run", ok=True)
        self.assertTrue(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(json.loads((self.statedir / "latest.json").read_text()), latest)
        self.assertEqual(json.loads((self.statedir / "result.json").read_text())["result"], "skipped")
        self.assertEqual(self.calls("copyto"), [])
        self.assertFalse((self.backups / "snapshots" / "orphan-01").exists())
        self.assertNotIn("Копирование завершено", result.stdout + result.stderr)
        self.assertNotIn("скачан и проверен", result.stdout + result.stderr)
        self.statedir.joinpath("enabled").touch()
        count = len(self.calls())
        self.invoke("tick", ok=True)
        self.assertEqual(len(self.calls()), count)

    def test_check_only_orphans_preserves_last_result_and_local_history(self):
        old = self.local_snapshot("old-01", 45)
        self.snapshot("orphan-01", sidecar=False)
        previous = {"result": "success", "at": NOW - 86400}
        write_json(self.statedir / "result.json", previous)
        self.invoke("check", ok=True)
        self.assertTrue(old.exists())
        self.assertEqual(json.loads((self.statedir / "result.json").read_text()), previous)
        self.assertEqual(self.calls("copyto"), [])

    def test_manifest_without_archive_still_fails_and_blocks_retention(self):
        old = self.local_snapshot("old-01", 45)
        self.snapshot("missing-01")
        self.remote.joinpath("missing-01.tar.zst").unlink()
        self.snapshot("valid-01")
        self.invoke("run", ok=False)
        self.assertTrue(old.exists())
        self.assertFalse((self.backups / "snapshots" / "missing-01").exists())
        self.assertEqual(json.loads((self.statedir / "result.json").read_text())["result"], "failed")

    def test_checksum_mismatch_never_publishes_or_prunes(self):
        old = self.local_snapshot("old-01", 45)
        self.snapshot("corrupt-01", mutate={"sha256Ciphertext": "0" * 64})
        self.invoke("run", ok=False)
        self.assertFalse((self.backups / "snapshots" / "corrupt-01" / ".verified.json").exists())
        self.assertTrue(old.exists())

    def test_size_mismatch_never_publishes(self):
        self.snapshot("wrong-size", mutate={"sizeBytes": 98765})
        self.invoke("run", ok=False)
        self.assertFalse((self.backups / "snapshots" / "wrong-size" / ".verified.json").exists())

    def test_corrupted_existing_local_archive_is_not_silently_overwritten(self):
        manifest, _ = self.snapshot()
        self.invoke("run", ok=True)
        dest = self.backups / "snapshots" / manifest["snapshotId"] / "snapshot-01.tar.zst"
        dest.write_bytes(b"local corruption")
        before = len(self.calls("copyto"))
        self.invoke("run", ok=False)
        self.assertEqual(dest.read_bytes(), b"local corruption")
        self.assertEqual(len([x for x in self.calls("copyto")[before:] if x["source"].endswith(".tar.zst")]), 0)

    def test_changed_remote_snapshot_preserves_local_copy(self):
        _, payload = self.snapshot()
        self.invoke("run", ok=True)
        self.snapshot(payload=b"replacement remote bytes")
        self.invoke("run", ok=False)
        self.assertEqual((self.backups / "snapshots" / "snapshot-01" / "snapshot-01.tar.zst").read_bytes(), payload)

    def test_retention_uses_snapshot_time_not_file_mtime(self):
        old = self.local_snapshot("old-01", 45)
        kept = self.local_snapshot("kept-01", 3)
        for path in kept.rglob("*"):
            os.utime(path, (NOW - 80 * 86400, NOW - 80 * 86400))
        self.snapshot("new-01", 0)
        self.invoke("run", ok=True)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())
        self.assertTrue((self.backups / "snapshots" / "new-01").exists())

    def test_stale_source_keeps_last_local_archive(self):
        self.snapshot("last-old", 45)
        self.invoke("run", ok=False)
        self.assertTrue((self.backups / "snapshots" / "last-old" / "last-old.tar.zst").exists())

    def test_failed_cloud_listing_preserves_all_local_history(self):
        old = self.local_snapshot("old-01", 45)
        write_json(self.controlfile, {"fail_all": True})
        self.invoke("run", ok=False)
        self.assertTrue(old.exists())

    def test_missing_disk_refuses_before_network(self):
        self.mounts.write_text(f"/dev/entware {self.prefix} ext4 rw 0 0\n")
        self.invoke("run", ok=False)
        self.assertEqual(self.calls(), [])

    def test_replaced_disk_uuid_refuses_before_network(self):
        write_json(self.uuids, {"/dev/entware": "entware-uuid", "/dev/hdd": "another-disk"})
        self.invoke("run", ok=False)
        self.assertEqual(self.calls(), [])

    def test_same_filesystem_as_entware_refuses(self):
        write_json(self.uuids, {"/dev/entware": "hdd-uuid", "/dev/hdd": "hdd-uuid"})
        self.invoke("run", ok=False)
        self.assertEqual(self.calls(), [])

    def test_keenetic_native_ntfs_accepts_only_matching_disk_with_private_modes(self):
        # KN-1811 reports its native NTFS driver as tntfs, not ntfs or fuseblk.
        self.mounts.write_text(f"/dev/entware {self.prefix} ext4 rw 0 0\n/dev/hdd {self.hdd} tntfs rw 0 0\n")
        self.snapshot()
        self.invoke("run", ok=True)
        dest = self.backups / "snapshots" / "snapshot-01"
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o700)
        self.assertEqual((dest / "snapshot-01.tar.zst").stat().st_mode & 0o777, 0o600)
        write_json(self.uuids, {"/dev/entware": "entware-uuid", "/dev/hdd": "replacement-uuid"})
        before = len(self.calls())
        self.invoke("run", ok=False)
        self.assertEqual(len(self.calls()), before)

    def test_native_ntfs_rejects_ineffective_private_permissions(self):
        self.mounts.write_text(f"/dev/entware {self.prefix} ext4 rw 0 0\n/dev/hdd {self.hdd} tntfs rw 0 0\n")
        native_stat = shutil.which("stat")
        self.make_executable("stat", f"#!{sys.executable}\nimport os,sys\nif sys.argv[1:] == ['-c', '%a', {str(self.backups)!r}]:\n print('777')\nelse:\n os.execv({native_stat!r}, [{native_stat!r}] + sys.argv[1:])\n")
        self.snapshot()
        self.invoke("run", ok=False)
        self.assertEqual(self.calls(), [])

    def test_symlink_destination_does_not_escape_disk(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.backups / "snapshots").symlink_to(outside, target_is_directory=True)
        self.snapshot()
        self.invoke("run", ok=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_untrusted_manifest_paths_and_fields_are_rejected(self):
        attacks = [
            {"snapshotId": "../outside"}, {"objectKey": "backups/../../outside.tar.zst"},
            {"objectKey": "other/snapshot-01.tar.zst"}, {"bucket": "another-bucket"},
            {"sizeBytes": -1}, {"sizeBytes": "34"}, {"sha256Ciphertext": "not-a-hash"},
            {"snapshotAt": "not-a-date"}, {"snapshotAt": "2099-01-01T00:00:00Z"},
        ]
        for attack in attacks:
            with self.subTest(attack=attack):
                self.snapshot(mutate=attack)
                self.invoke("run", ok=False)
                self.assertFalse((self.backups / "snapshots" / "snapshot-01" / ".verified.json").exists())

    def test_config_is_data_never_shell_code(self):
        sentinel = self.base / "executed"
        self.config["prefix"] = "$(touch " + str(sentinel) + ")"
        self.save_config()
        self.invoke("run", ok=False)
        self.assertFalse(sentinel.exists())

    def test_mismatched_s3_configuration_refuses_before_network(self):
        self.snapshot()
        rconf = self.configdir / "rclone.conf"
        rconf.write_text(rconf.read_text().replace("https://s3.example.invalid", "https://wrong.example.invalid"))
        self.invoke("run", ok=False)
        self.assertEqual(self.calls(), [])

    def test_disk_space_failure_never_publishes(self):
        self.config["reserve_mib"] = 1048576
        self.save_config()
        self.snapshot()
        self.invoke("run", ok=False)
        self.assertFalse((self.backups / "snapshots" / "snapshot-01" / ".verified.json").exists())

    def test_disabled_tick_does_not_contact_s3(self):
        self.invoke("tick", ok=True)
        self.assertEqual(self.calls(), [])

    def test_status_does_not_contact_s3_or_disclose_secrets(self):
        self.invoke("status", ok=True)
        self.assertEqual(self.calls(), [])

    def test_scheduler_polls_once_per_day_after_configured_local_time(self):
        self.snapshot()
        self.statedir.joinpath("enabled").touch()
        env = self.env.copy()
        # 01:59 UTC is 04:59 Moscow; the configured time is 05:00.
        day = NOW - 12 * 3600
        env["S3BACKUP_NOW"] = str(day + 119 * 60)
        self.invoke("tick", env=env, ok=True)
        self.assertEqual(self.calls(), [])
        # Use an archive from before the first scheduled execution.
        self.snapshot(age_days=1)
        env["S3BACKUP_NOW"] = str(day + 120 * 60)
        self.invoke("tick", env=env, ok=True)
        calls = len(self.calls())
        self.assertGreater(calls, 0)
        env["S3BACKUP_NOW"] = str(day + 121 * 60)
        self.invoke("tick", env=env, ok=True)
        self.assertEqual(len(self.calls()), calls)
        env["S3BACKUP_NOW"] = str(day + 86400 + 120 * 60)
        self.invoke("tick", env=env, ok=True)
        self.assertGreater(len(self.calls()), calls)

    def test_scheduler_failed_attempt_is_not_retried_every_minute(self):
        self.statedir.joinpath("enabled").touch()
        write_json(self.controlfile, {"fail_all": True})
        self.invoke("tick", ok=False)
        count = len(self.calls())
        self.assertGreater(count, 0)
        later = self.env.copy()
        later["S3BACKUP_NOW"] = str(NOW + 60)
        self.invoke("tick", env=later, ok=True)
        self.assertEqual(len(self.calls()), count)

    def test_scheduler_interval_and_late_start_use_calendar_days(self):
        self.snapshot()
        self.config["interval_days"] = 2
        self.save_config()
        self.statedir.joinpath("enabled").touch()
        self.invoke("tick", ok=True)
        count = len(self.calls())
        next_day = self.env.copy()
        next_day["S3BACKUP_NOW"] = str(NOW + 86400)
        self.invoke("tick", env=next_day, ok=True)
        self.assertEqual(len(self.calls()), count)
        next_day["S3BACKUP_NOW"] = str(NOW + 2 * 86400 - 9 * 3600)
        self.invoke("tick", env=next_day, ok=True)
        self.assertGreater(len(self.calls()), count)

    def test_corruption_in_cloud_expired_local_archive_blocks_pruning(self):
        old = self.local_snapshot("old-01", 45)
        old.joinpath("old-01.tar.zst").write_bytes(b"corrupt offline archive")
        self.snapshot("fresh-01")
        self.invoke("run", ok=False)
        self.assertTrue(old.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires actual Linux /proc and process identity")
    def test_stop_terminates_own_download_preserves_unrelated_process(self):
        self.statedir.joinpath("enabled").touch()
        worker, child_pid = self.start_slow_worker()
        unrelated = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self.cleanup_worker, unrelated)
        self.invoke("stop", ok=True, timeout=25)
        worker.communicate(timeout=5)
        self.assertIsNotNone(worker.returncode)
        self.assertNotEqual(worker.returncode, 0)
        self.assertFalse(Path(f"/proc/{child_pid}").exists())
        self.assertIsNone(unrelated.poll())
        self.assertFalse(self.statedir.joinpath("enabled").exists())
        self.assertFalse((self.backups / "snapshots" / "snapshot-01" / ".verified.json").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires actual Linux /proc and process identity")
    def test_concurrent_manual_run_refuses_with_failure_status(self):
        worker, _ = self.start_slow_worker()
        self.invoke("run", ok=False)
        self.assertIsNone(worker.poll())

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires actual Linux /proc and process identity")
    def test_configure_during_copy_fails_without_touching_config(self):
        before = (self.configdir / "config.json").read_bytes()
        worker, _ = self.start_slow_worker()
        self.invoke("configure", ok=False)
        self.assertEqual((self.configdir / "config.json").read_bytes(), before)
        self.assertIsNone(worker.poll())

    def test_wizard_changes_settings_and_hides_secret_input(self):
        new_secret = "new_FAKE_secret$with=punctuation"
        answers = [
            ("Адрес S3", "https://new.example.invalid"), ("Регион", "ru-central-1"),
            ("Бакет", "newbucket"), ("Папка в бакете", "newprefix"),
            ("Адресация path-style", "false"), ("Заменить ключи S3?", "да"),
            ("Access Key (ввод скрыт)", "fake:access"), ("Secret Key (ввод скрыт)", new_secret),
            ("Полный путь", str(self.hdd / "new-backups")), ("Время ежедневной", "06:30"),
            ("Часовой пояс", "MSK-3"), ("Интервал в днях", "2"),
            ("Хранить снимки", "45"), ("Ограничение скорости", "3"),
            ("Сохранить настройки?", "да"),
        ]
        code, output = self.interactive(["bash", str(self.cli), "configure"], answers)
        self.assertEqual(code, 0, output)
        self.assertNotIn(new_secret, output)
        updated = json.loads((self.configdir / "config.json").read_text())
        self.assertEqual((updated["time"], updated["interval_days"], updated["retention_days"]), ("06:30", 2, 45))
        self.assertEqual(updated["endpoint"], "https://new.example.invalid")
        text = (self.configdir / "rclone.conf").read_text()
        self.assertIn("secret_access_key = " + new_secret, text)
        self.assertEqual((self.configdir / "rclone.conf").stat().st_mode & 0o777, 0o600)

    def directory_wizard_answers(self, directory):
        return [
            ("Адрес S3", ""), ("Регион", ""), ("Бакет", ""),
            ("Папка в бакете", ""), ("Адресация path-style", ""),
            ("Заменить ключи S3?", "нет"), ("Полный путь", str(directory)),
            ("Время ежедневной", ""), ("Часовой пояс", ""),
            ("Интервал в днях", ""), ("Хранить снимки", ""),
            ("Ограничение скорости", ""), ("Сохранить настройки?", "да"),
        ]

    def test_wizard_adopts_existing_empty_directory_after_restricting_permissions(self):
        # The user may create the HDD folder before entering the wizard.
        directory = self.hdd / "already-created"
        directory.mkdir(mode=0o755)
        code, output = self.interactive(["bash", str(self.cli), "configure"], self.directory_wizard_answers(directory))
        self.assertEqual(code, 0, output)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / ".tkbackup-owner").read_text().strip(), "tkbackup-v1")
        self.assertEqual(json.loads((self.configdir / "config.json").read_text())["backup_dir"], str(directory))

    def test_fresh_wizard_accepts_explicit_s3_target_and_uses_neutral_defaults(self):
        (self.configdir / "config.json").unlink()
        (self.configdir / "rclone.conf").unlink()
        answers = [
            ("Адрес S3 (HTTPS) []", "https://s3.example.invalid"),
            ("Регион [us-east-1]", ""), ("Бакет []", "testbucket"),
            ("Папка в бакете [backups]", ""), ("Адресация path-style", ""),
            ("Access Key", "FAKE_ACCESS_KEY"), ("Secret Key", self.secret),
            ("Полный путь", str(self.backups)), ("Время ежедневной", ""),
            ("Часовой пояс", ""), ("Интервал в днях", ""),
            ("Хранить снимки", ""), ("Ограничение скорости", ""),
            ("Сохранить настройки?", "да"),
        ]
        code, output = self.interactive(["bash", str(self.cli), "configure"], answers)
        self.assertEqual(code, 0, output)
        self.assertNotIn(self.secret, output)
        config = json.loads((self.configdir / "config.json").read_text())
        self.assertEqual(config["endpoint"], "https://s3.example.invalid")
        self.assertEqual(config["bucket"], "testbucket")
        self.assertEqual(config["region"], "us-east-1")
        self.assertEqual(config["timezone"], "UTC0")
        self.assertEqual(config["time"], "05:00")
        self.assertEqual(config["retention_days"], 30)
        self.assertEqual(config["interval_days"], 1)
        self.assertEqual((self.backups / ".tkbackup-owner").read_text().strip(), "tkbackup-v1")

    def test_wizard_refuses_nonempty_foreign_directory_including_hidden_files(self):
        directory = self.hdd / "foreign-folder"
        directory.mkdir()
        directory.joinpath(".keep").write_text("owner's unrelated data")
        original = (self.configdir / "config.json").read_bytes()
        code, output = self.interactive(["bash", str(self.cli), "configure"], self.directory_wizard_answers(directory))
        self.assertNotEqual(code, 0, output)
        self.assertEqual(directory.joinpath(".keep").read_text(), "owner's unrelated data")
        self.assertFalse(directory.joinpath(".tkbackup-owner").exists())
        self.assertEqual((self.configdir / "config.json").read_bytes(), original)

    def test_uninstall_preserves_archives_config_and_other_cron_jobs(self):
        old = self.local_snapshot("old-01", 1)
        cron = self.prefix / "etc" / "cron.d"
        cron.mkdir(parents=True)
        cron.joinpath("s3backup").write_text("owned job")
        cron.joinpath("unrelated").write_text("other job")
        self.prefix.joinpath("bin", "s3backup").write_text("owned executable")
        self.prefix.joinpath("etc", "init.d", "S98s3backup").write_text("owned init")
        code, output = self.interactive(["bash", str(self.cli), "uninstall"], [("Удалить программу", "да")])
        self.assertEqual(code, 0, output)
        self.assertFalse(cron.joinpath("s3backup").exists())
        self.assertEqual(cron.joinpath("unrelated").read_text(), "other job")
        self.assertTrue(old.joinpath("old-01.tar.zst").exists())
        self.assertTrue((self.configdir / "rclone.conf").exists())

    def test_init_start_preserves_explicit_disabled_state(self):
        init = self.base / "S98s3backup"
        init.write_text((PROJECT / "S98s3backup").read_text().replace("@PREFIX@", str(self.prefix)))
        result = subprocess.run(["sh", str(init), "start"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.statedir.joinpath("enabled").exists())
        self.assertEqual(self.calls(), [])

    def test_shutdown_preserves_enabled_state_for_next_boot(self):
        self.statedir.joinpath("enabled").touch()
        self.invoke("shutdown", ok=True)
        self.assertTrue(self.statedir.joinpath("enabled").exists())
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires actual Linux /proc and process identity")
    def test_stop_does_not_signal_reused_pid_from_previous_boot(self):
        unrelated = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self.cleanup_worker, unrelated)
        raw = Path(f"/proc/{unrelated.pid}/stat").read_text()
        started = raw.rsplit(") ", 1)[1].split()[19]
        write_json(self.rundir / "process.json", {"pid": unrelated.pid, "token": "previous-boot-id:" + started})
        self.statedir.joinpath("enabled").touch()
        self.invoke("stop", ok=False)
        self.assertIsNone(unrelated.poll())
        self.assertFalse(self.statedir.joinpath("enabled").exists())

    def test_changing_s3_source_requires_separate_local_directory(self):
        self.snapshot()
        self.invoke("run", ok=True)
        before = len(self.calls())
        self.config["endpoint"] = "https://another.example.invalid"
        self.save_config()
        self.invoke("run", ok=False)
        self.assertEqual(len(self.calls()), before)
        self.assertTrue((self.backups / "snapshots" / "snapshot-01" / ".verified.json").exists())

    def test_expired_source_objects_are_not_redownloaded_after_pruning(self):
        old = self.local_snapshot("old-01", 45)
        self.snapshot("old-01", 45)
        self.snapshot("fresh-01")
        self.invoke("run", ok=True)
        self.assertFalse(old.exists())
        self.callsfile.write_text("")
        self.invoke("run", ok=True)
        self.assertFalse(old.exists())
        self.assertFalse(any(call["source"].endswith("/old-01.tar.zst") for call in self.calls("copyto")))

    def test_failed_transfer_leaves_no_unbounded_staging_data(self):
        self.snapshot("corrupt-01", mutate={"sha256Ciphertext": "0" * 64})
        for _ in range(2):
            self.invoke("run", ok=False)
        staging = self.backups / ".staging"
        self.assertEqual(list(staging.iterdir()), [])

    def prepare_installer(self):
        self.prefix.joinpath("bin", "bash").symlink_to(shutil.which("bash"))
        opkg_log = self.base / "opkg.jsonl"
        self.make_executable("opkg", f"#!{sys.executable}\nimport json,sys\nwith open({str(opkg_log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
        return opkg_log

    def remote_installer(self, fail=""):
        bootstrap = self.base / "bootstrap"
        bootstrap.mkdir()
        script = bootstrap / "install.sh"
        shutil.copyfile(PROJECT / "install.sh", script)
        self.make_executable("curl", f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(HERE / 'fake_curl.py')!r}, run_name='__main__')\n")
        self.env.update({"FAKE_CURL_LOG": str(self.base / "curl.jsonl"), "FAKE_CURL_SOURCE": str(PROJECT), "FAKE_CURL_FAIL": fail})
        return ["sh", str(script), "--download"]

    def exercise_installer(self, download=False):
        opkg_log = self.prepare_installer()
        cron = self.prefix / "etc" / "cron.d"
        cron.mkdir(parents=True)
        cron.joinpath("unrelated").write_text("other job")
        answers = [
            ("Установить s3backup", "да"), ("Адрес S3", ""), ("Регион", ""),
            ("Бакет", ""), ("Папка в бакете", ""), ("Адресация path-style", ""),
            ("Заменить ключи S3?", "нет"), ("Полный путь", ""),
            ("Время ежедневной", ""), ("Часовой пояс", ""),
            ("Интервал в днях", ""), ("Хранить снимки", ""),
            ("Ограничение скорости", ""), ("Сохранить настройки?", "да"),
            ("Включить ежедневное копирование?", "да"),
        ]
        argv = self.remote_installer() if download else ["sh", str(PROJECT / "install.sh")]
        code, output = self.interactive(argv, answers)
        self.assertEqual(code, 0, output)
        calls = [json.loads(x) for x in opkg_log.read_text().splitlines()]
        self.assertEqual(calls[0], ["update"])
        self.assertTrue(any(call[0] == "install" and "rclone" in call for call in calls))
        self.assertFalse(any("upgrade" in call or "remove" in call for call in calls))
        self.assertEqual(cron.joinpath("unrelated").read_text(), "other job")
        self.assertIn("s3backup tick", cron.joinpath("s3backup").read_text())
        self.assertTrue(self.statedir.joinpath("enabled").exists())
        self.assertTrue(self.prefix.joinpath("bin", "s3backup").stat().st_mode & 0o111)
        self.assertTrue(self.prefix.joinpath("etc", "init.d", "S98s3backup").stat().st_mode & 0o111)
        self.assertEqual(self.calls(), [])
        if download:
            calls = [json.loads(line) for line in (self.base / "curl.jsonl").read_text().splitlines()]
            self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in calls], ["s3backup", "S98s3backup"])
            for call in calls:
                self.assertIn("--fail", call["argv"])
                self.assertIn("=https", call["argv"])
                self.assertFalse(Path(call["destination"]).exists(), "download staging must be cleaned")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "installer requires root in disposable Linux container")
    def test_installer_only_installs_prerequisites_and_owns_its_cron_entry(self):
        self.exercise_installer()

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "installer requires root in disposable Linux container")
    def test_remote_installer_downloads_both_files_and_installs(self):
        self.exercise_installer(download=True)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "installer requires root in disposable Linux container")
    def test_remote_installer_http_failure_does_not_modify_entware(self):
        opkg_log = self.prepare_installer()
        argv = self.remote_installer(fail="S98s3backup")
        before = {str(p.relative_to(self.prefix)): (p.read_bytes() if p.is_file() else None, p.stat().st_mode) for p in self.prefix.rglob("*")}
        code, output = self.interactive(argv, [])
        self.assertNotEqual(code, 0, output)
        self.assertFalse(opkg_log.exists())
        after = {str(p.relative_to(self.prefix)): (p.read_bytes() if p.is_file() else None, p.stat().st_mode) for p in self.prefix.rglob("*")}
        self.assertEqual(after, before)
        calls = [json.loads(line) for line in (self.base / "curl.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(not Path(call["destination"]).exists() for call in calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
