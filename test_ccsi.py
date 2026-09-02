import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).with_name("ccsi")


def run_ccsi(home, *args):
  env = os.environ.copy()
  env["HOME"] = str(home)
  return subprocess.run(
    [str(SCRIPT), *args],
    env=env,
    capture_output=True,
    text=True,
    check=True,
  )


class IndexSafetyTest(unittest.TestCase):
  def test_read_failure_preserves_existing_index_and_metadata(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      session = home / ".claude/projects/probe/session.jsonl"
      session.parent.mkdir(parents=True)
      session.write_text(json.dumps({
        "timestamp": "2026-08-14T00:00:00Z",
        "message": {"role": "user", "content": "oldneedle"},
      }) + "\n")
      run_ccsi(home, "index")

      db_path = home / "Library/Caches/cc-session-index/index.sqlite"
      with closing(sqlite3.connect(db_path)) as db:
        before_mtime = db.execute(
          "SELECT mtime FROM files WHERE path = ?",
          (str(session),),
        ).fetchone()[0]
        before_count = db.execute(
          "SELECT count(*) FROM messages WHERE messages MATCH 'oldneedle'",
        ).fetchone()[0]

      changed_mtime = session.stat().st_mtime + 10
      os.utime(session, (changed_mtime, changed_mtime))
      session.chmod(0)
      try:
        run_ccsi(home, "index")
      finally:
        session.chmod(0o600)

      with closing(sqlite3.connect(db_path)) as db:
        after_mtime = db.execute(
          "SELECT mtime FROM files WHERE path = ?",
          (str(session),),
        ).fetchone()[0]
        after_count = db.execute(
          "SELECT count(*) FROM messages WHERE messages MATCH 'oldneedle'",
        ).fetchone()[0]

      self.assertEqual(before_count, 1)
      self.assertEqual(after_count, 1)
      self.assertEqual(after_mtime, before_mtime)

  def test_changed_file_replaces_existing_messages(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      session = home / ".claude/projects/probe/session.jsonl"
      session.parent.mkdir(parents=True)
      session.write_text(json.dumps({
        "timestamp": "2026-09-02T00:00:00Z",
        "message": {"role": "user", "content": "oldneedle"},
      }) + "\n")
      run_ccsi(home, "index")

      session.write_text(json.dumps({
        "timestamp": "2026-09-02T00:01:00Z",
        "message": {"role": "user", "content": "newneedle"},
      }) + "\n")
      changed_mtime = session.stat().st_mtime + 10
      os.utime(session, (changed_mtime, changed_mtime))
      run_ccsi(home, "index")

      db_path = home / "Library/Caches/cc-session-index/index.sqlite"
      with closing(sqlite3.connect(db_path)) as db:
        old_count = db.execute(
          "SELECT count(*) FROM messages WHERE messages MATCH 'oldneedle'",
        ).fetchone()[0]
        new_count = db.execute(
          "SELECT count(*) FROM messages WHERE messages MATCH 'newneedle'",
        ).fetchone()[0]

      self.assertEqual(old_count, 0)
      self.assertEqual(new_count, 1)


class IndexPerformanceTest(unittest.TestCase):
  def test_initial_build_avoids_quadratic_fts_scans(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      root = home / ".claude/projects/probe"
      root.mkdir(parents=True)
      line = json.dumps({
        "timestamp": "2026-09-02T00:00:00Z",
        "message": {"role": "user", "content": "benchmark needle"},
      }) + "\n"
      for index in range(15_000):
        (root / f"{index:05d}.jsonl").write_text(line)

      started = time.monotonic()
      run_ccsi(home, "index")
      elapsed = time.monotonic() - started

      db_path = home / "Library/Caches/cc-session-index/index.sqlite"
      with closing(sqlite3.connect(db_path)) as db:
        file_count = db.execute("SELECT count(*) FROM files").fetchone()[0]
        message_count = db.execute("SELECT count(*) FROM messages").fetchone()[0]

      self.assertEqual(file_count, 15_000)
      self.assertEqual(message_count, 15_000)
      self.assertLess(elapsed, 10, f"initial index took {elapsed:.1f}s")


if __name__ == "__main__":
  unittest.main()
