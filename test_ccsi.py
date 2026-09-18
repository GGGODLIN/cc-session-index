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


def write_codex_rollout(path, session_id="01a0aaaa-0000-7000-8000-000000000001"):
  path.parent.mkdir(parents=True, exist_ok=True)
  rows = [
    {"timestamp": "2026-09-13T00:00:00Z", "type": "session_meta", "payload": {"id": session_id, "cwd": "/tmp/proj", "originator": "Codex Desktop", "source": {"subagent": {"thread_spawn": {"agent_role": "cc_judge"}}}}},
    {"timestamp": "2026-09-13T00:00:01Z", "type": "turn_context", "payload": {"cwd": "/tmp/proj", "model": "gpt-5.6-luna", "effort": "max"}},
    {"timestamp": "2026-09-13T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "codexneedle please"}]}},
    {"timestamp": "2026-09-13T00:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent", "arguments": json.dumps({"agent_type": "cc_impl", "message": "do it"})}},
    {"timestamp": "2026-09-13T00:00:04Z", "type": "response_item", "payload": {"type": "function_call_output", "output": "spawned"}},
    {"timestamp": "2026-09-13T00:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done codexreply"}]}},
  ]
  path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class DualVendorTest(unittest.TestCase):
  def test_codex_rollouts_are_indexed_and_tagged(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      cc = home / ".claude/projects/probe/session.jsonl"
      cc.parent.mkdir(parents=True)
      cc.write_text(json.dumps({"type": "user", "timestamp": "2026-09-13T00:00:00Z", "message": {"role": "user", "content": "ccneedle here"}}) + "\n")
      write_codex_rollout(home / ".codex/sessions/2026/09/13/rollout-2026-09-13T00-00-00-01a0aaaa.jsonl")
      write_codex_rollout(home / ".codex/archived_sessions/rollout-2026-09-01T00-00-00-01a0bbbb.jsonl", "01a0bbbb-0000-7000-8000-000000000002")
      out = run_ccsi(home, "index").stdout
      self.assertIn("scanned=3 updated=3", out)

      hits = run_ccsi(home, "search", "codexneedle").stdout
      self.assertEqual(hits.count("--- [codex]"), 2)
      self.assertIn("rollout-2026-09-01T00-00-00-01a0bbbb.jsonl", hits)
      self.assertIn("--- [cc] probe/session.jsonl", run_ccsi(home, "search", "ccneedle").stdout)
      self.assertIn("[>>>spawn_agent<<<] {", run_ccsi(home, "search", "spawn_agent").stdout)
      self.assertEqual(run_ccsi(home, "search", "codexneedle", "--vendor", "cc").stdout.strip(), "(no hits)")
      self.assertEqual(run_ccsi(home, "search", "ccneedle", "--vendor", "codex").stdout.strip(), "(no hits)")

  def test_events_stream_normalizes_both_formats(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      cc = home / ".claude/projects/probe/abc.jsonl"
      cc.parent.mkdir(parents=True)
      cc.write_text(
        json.dumps({"type": "user", "sessionId": "abc", "cwd": "/tmp/proj", "timestamp": "2026-09-13T00:00:00Z", "message": {"role": "user", "content": "hello"}}) + "\n"
        + json.dumps({"type": "assistant", "sessionId": "abc", "timestamp": "2026-09-13T00:00:01Z", "message": {"role": "assistant", "model": "claude-fable-5-1", "content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"subagent_type": "routed-judge", "model": "opus", "prompt": "judge"}}]}}) + "\n"
      )
      write_codex_rollout(home / ".codex/sessions/2026/09/13/rollout-x.jsonl")
      events = [json.loads(l) for l in run_ccsi(home, "events").stdout.splitlines()]
      vendors = {e["vendor"] for e in events}
      self.assertEqual(vendors, {"cc", "codex"})
      cc_agent = [e for e in events if e["vendor"] == "cc" and e["kind"] == "tool_call"][0]
      self.assertEqual((cc_agent["tool"], cc_agent["agent_type"], cc_agent["agent_model"], cc_agent["model"]), ("Agent", "routed-judge", "opus", "claude-fable-5-1"))
      spawn = [e for e in events if e["vendor"] == "codex" and e["kind"] == "tool_call"][0]
      self.assertEqual((spawn["tool"], spawn["agent_type"], spawn["model"], spawn["effort"], spawn["agent_role"], spawn["cwd"]), ("spawn_agent", "cc_impl", "gpt-5.6-luna", "max", "cc_judge", "/tmp/proj"))
      only_tool = run_ccsi(home, "events", "--vendor", "codex", "--kind", "tool_call", "--tool", "spawn_agent").stdout.splitlines()
      self.assertEqual(len(only_tool), 1)
      self.assertEqual(run_ccsi(home, "events", "--since", "2027-01-01").stdout, "")


class StructuredToolInputTest(unittest.TestCase):
  def test_object_valued_tool_arguments_are_indexed_without_crashing(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      session = home / ".claude/projects/probe/structured.jsonl"
      session.parent.mkdir(parents=True)
      session.write_text(
        json.dumps({
          "type": "assistant",
          "sessionId": "structured",
          "timestamp": "2026-09-19T00:00:00Z",
          "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__mongodb__count", "input": {"query": {"endDate": None, "tag": "objectneedle"}}},
            {"type": "tool_use", "id": "t2", "name": "Artifact", "input": {"query": {"limit": 5, "cursor": "cursorneedle"}}},
          ]},
        }) + "\n"
        + json.dumps({
          "type": "assistant",
          "sessionId": "structured",
          "timestamp": "2026-09-19T00:00:01Z",
          "message": {"role": "assistant", "content": [
            {"type": "text", "text": {"unexpected": "textneedle"}},
          ]},
        }) + "\n"
      )

      run_ccsi(home, "index")

      for needle in ("objectneedle", "cursorneedle", "textneedle"):
        self.assertIn("structured.jsonl", run_ccsi(home, "search", needle).stdout, needle)


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
