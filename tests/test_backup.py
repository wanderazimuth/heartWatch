import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from heartwatch import backup, db, store
from heartwatch.config import Config

from fitwriter import write

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


class BackupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = Config(db_path=str(self.root / "hw.db"), archive_dir=str(self.root / "archive"),
                          backup_target="nas:/backup/hw/")
        conn = db.connect(self.cfg.db_path)
        store.import_fit(conn, self.cfg, "run.fit", write(START, [150] * 600))
        conn.close()

    def test_snapshot_is_a_complete_database(self):
        copy = backup.snapshot(self.cfg)
        conn = sqlite3.connect(copy)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 1)

    def test_rsync_never_deletes(self):
        cmds = backup.rsync_commands(self.cfg, Path("snap.db"), self.cfg.backup_target)
        self.assertEqual(cmds[0][-1], "nas:/backup/hw/archive/")
        self.assertIn("-e", cmds[0])
        self.assertFalse(any("--delete" in arg for cmd in cmds for arg in cmd))

    def test_a_mounted_share_is_copied_to_locally(self):
        cmds = backup.rsync_commands(self.cfg, Path("snap.db"), "/mnt/heartwatch/")
        self.assertEqual(cmds[1][-2:], ["snap.db", "/mnt/heartwatch/heartwatch.db"])
        self.assertNotIn("-e", cmds[0])

    def test_share_must_really_be_mounted(self):
        mounts = self.root / "mounts"
        mounts.write_text("/dev/root / ext4 rw 0 0\nsystemd-1 /mnt/heartwatch autofs rw 0 0\n")
        self.assertFalse(backup.mounted("/mnt/heartwatch", str(mounts)))
        mounts.write_text("//nas/heartwatch /mnt/heartwatch cifs rw 0 0\n")
        self.assertTrue(backup.mounted("/mnt/heartwatch", str(mounts)))
        cfg = Config(db_path=self.cfg.db_path, archive_dir=self.cfg.archive_dir, backup_share="//nas/heartwatch")
        with self.assertRaises(SystemExit):
            backup.run(cfg, mount=str(self.root / "not-mounted"))

    def test_archive_can_move(self):
        # Paths are stored relative to archive_dir, so a copied archive (Windows to the Pi) still works.
        moved = self.root / "elsewhere"
        shutil.copytree(self.cfg.archive_dir, moved)
        shutil.rmtree(self.cfg.archive_dir)
        cfg = Config(db_path=self.cfg.db_path, archive_dir=str(moved))
        conn = db.connect(cfg.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(store.rebuild_all(conn, cfg), 1)


if __name__ == "__main__":
    unittest.main()
