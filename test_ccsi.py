import json
import os
import sqlite3
import subprocess
import tempfile
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


if __name__ == "__main__":
  unittest.main()
