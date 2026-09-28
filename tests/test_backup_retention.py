#!/usr/bin/env python3

"""Backup-dir retention for the storage layer.

``ResultManager.backup_existing_files`` runs on EVERY run start
(``manager/task.py``), and ``search``-side result files are moved into a
``backup-<YYYYmmdd-HHMMSS>`` dir each time. Before this change nothing ever
pruned those dirs, so prod accumulated 76+ per provider. ``prune_backup_dirs``
now keeps the newest ``_BACKUP_RETENTION`` and only ever touches dirs matching
the exact ``backup-<timestamp>`` naming inside the provider's own directory —
siblings like ``data/_attic/`` are never at risk (different dir + different
name).

Every test uses a tmp dir; no network, no real providers beyond a name/layout
stub.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import Dict, List
from unittest import mock

from core.models import ResultStorage
from storage.persistence import (
    _BACKUP_DIR_RE,
    _BACKUP_RETENTION,
    ResultManager,
    prune_backup_dirs,
)

_PROVIDER_FILENAMES: Dict[str, str] = {
    "valid": "valid-keys.txt",
    "no_quota": "no-quota-keys.txt",
    "wait_check": "wait-check-keys.txt",
    "invalid": "invalid-keys.txt",
    "material": "material.txt",
    "summary": "summary.json",
    "links": "links.txt",
}

# Seed stamps are year-2000 so a fresh backup (real ``now``) is always newest —
# keeps the integration assertions clock-independent.
_SEEDS: List[str] = [
    "backup-20000101-000000",
    "backup-20000201-000000",
    "backup-20000301-000000",
    "backup-20000401-000000",
    "backup-20000501-000000",
]


class _StubProvider:
    """Minimal IProvider stand-in: only name + result layout are read."""

    def __init__(self, name: str = "prov") -> None:
        self.name = name
        self.result = ResultStorage(folder=name, filenames=dict(_PROVIDER_FILENAMES))


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp()
        self.managers: List[ResultManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            try:
                manager.stop()
            except Exception:
                pass
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _backup_dirs(self, directory: str) -> List[str]:
        return sorted(
            entry
            for entry in os.listdir(directory)
            if _BACKUP_DIR_RE.match(entry) and os.path.isdir(os.path.join(directory, entry))
        )


class TestPruneBackupDirs(_TmpDirCase):
    """Unit coverage for the retention primitive."""

    def test_keeps_newest_and_spares_decoys(self) -> None:
        # Given 5 valid backup dirs plus a mix of look-alike decoys...
        for name in _SEEDS:
            os.makedirs(os.path.join(self.workspace, name))
        decoy_dirs = ["_attic", "backup-malformed", "backup-2000", "results", "backup-20000101-0000"]
        for name in decoy_dirs:
            os.makedirs(os.path.join(self.workspace, name))
        # ...and a FILE shaped like a backup dir (isdir guard must skip it).
        decoy_file = os.path.join(self.workspace, "backup-20000601-000000")
        with open(decoy_file, "w", encoding="utf-8") as fh:
            fh.write("not a directory\n")

        # When pruning
        removed = prune_backup_dirs(self.workspace, "prov")

        # Then exactly the surplus valid dirs are removed, newest kept.
        self.assertEqual(len(_SEEDS) - _BACKUP_RETENTION, removed)
        remaining = self._backup_dirs(self.workspace)
        self.assertEqual(_SEEDS[-_BACKUP_RETENTION:], remaining)
        for name in decoy_dirs:
            self.assertTrue(os.path.isdir(os.path.join(self.workspace, name)), f"{name} must survive")
        self.assertTrue(os.path.isfile(decoy_file), "a non-dir backup-named entry must survive")

    def test_noop_below_retention(self) -> None:
        os.makedirs(os.path.join(self.workspace, "backup-20000101-000000"))
        os.makedirs(os.path.join(self.workspace, "backup-20000201-000000"))

        removed = prune_backup_dirs(self.workspace, "prov")

        self.assertEqual(0, removed)
        self.assertEqual(2, len(self._backup_dirs(self.workspace)))

    def test_missing_directory_is_noop(self) -> None:
        removed = prune_backup_dirs(os.path.join(self.workspace, "does-not-exist"), "prov")
        self.assertEqual(0, removed)

    def test_locked_dir_is_swallowed_not_raised(self) -> None:
        # A rmtree failure on one stale dir must log a warning and never raise,
        # and must not stop the other stale dirs from being pruned.
        for name in _SEEDS:
            os.makedirs(os.path.join(self.workspace, name))
        with mock.patch(
            "storage.persistence.shutil.rmtree", side_effect=OSError(13, "locked")
        ):
            removed = prune_backup_dirs(self.workspace, "prov")  # must not raise
        self.assertEqual(0, removed, "a failed rmtree counts as not-removed")
        self.assertEqual(len(_SEEDS), len(self._backup_dirs(self.workspace)))


class TestBackupExistingFilesPrunes(_TmpDirCase):
    """Integration: the real backup path creates a dir and then prunes."""

    def test_backup_creates_new_dir_and_prunes_old(self) -> None:
        manager = ResultManager(
            _StubProvider("prov"),
            self.workspace,
            simple=True,
            save_interval=3600.0,
            shutdown_timeout=1.0,
        )
        self.managers.append(manager)

        for name in _SEEDS:
            os.makedirs(os.path.join(manager.directory, name))
        attic = os.path.join(manager.directory, "_attic")
        os.makedirs(attic)

        # A real result file must exist, else backup_existing_files early-returns
        # (nothing to back up) and never prunes.
        seeded = next(iter(manager.files.values()))
        with open(seeded, "w", encoding="utf-8") as fh:
            fh.write("sk-placeholder-not-a-real-key\n")

        manager.backup_existing_files()

        # 5 seeded + 1 fresh (now) = 6 created -> pruned to newest 3.
        remaining = self._backup_dirs(manager.directory)
        self.assertEqual(_BACKUP_RETENTION, len(remaining))
        self.assertNotIn(_SEEDS[0], remaining)
        self.assertNotIn(_SEEDS[1], remaining)
        self.assertNotIn(_SEEDS[2], remaining)
        self.assertIn(_SEEDS[4], remaining, "newest seeded dir must be kept")
        self.assertTrue(os.path.isdir(attic), "_attic sibling must be untouched")
        self.assertFalse(os.path.exists(seeded), "result file must have moved into the fresh backup")


if __name__ == "__main__":
    unittest.main()
