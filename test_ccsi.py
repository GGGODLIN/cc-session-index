import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).with_name("ccsi")


def db_path_for(home):
  if sys.platform == "darwin":
    return home / "Library/Caches/cc-session-index/index.sqlite"
  return home / ".cache/cc-session-index/index.sqlite"


LEGACY_SCRIPT = SCRIPT.parent / "testdata/ccsi-v2.py"


def ccsi_env(home, extra_env=None):
  env = os.environ.copy()
  env["HOME"] = str(home)
  for k in ("CCSI_DB_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR", "XDG_CACHE_HOME"):
    env.pop(k, None)
  if extra_env:
    env.update(extra_env)
  return env


def run_ccsi(home, *args, check=True, extra_env=None, script=SCRIPT):
  return subprocess.run(
    [sys.executable, str(script), *args],
    env=ccsi_env(home, extra_env),
    capture_output=True,
    text=True,
    check=check,
    timeout=20,
  )


def cc_session(home, name, text):
  path = home / ".claude/projects/probe" / f"{name}.jsonl"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps({
    "type": "user", "timestamp": "2026-10-19T00:00:00Z",
    "message": {"role": "user", "content": text},
  }) + "\n")
  return path


class ContentlessUpgradeTest(unittest.TestCase):
  def test_v2_upgrade_keeps_searchable_old_db_until_replace(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      cc_session(home, "old-session", "legacyneedle")
      run_ccsi(home, "index", script=LEGACY_SCRIPT)
      database = db_path_for(home)
      old_bytes = database.read_bytes()
      old_hit = run_ccsi(home, "search", "legacyneedle", script=LEGACY_SCRIPT).stdout
      self.assertIn("old-session.jsonl", old_hit)
      cc_session(home, "new-session", "rebuiltneedle")

      scoped = run_ccsi(home, "index", "--vendor", "cc", check=False)
      self.assertNotEqual(scoped.returncode, 0)
      self.assertIn("ccsi index", scoped.stderr)
      self.assertEqual(database.read_bytes(), old_bytes)
      self.assertEqual(run_ccsi(home, "search", "legacyneedle").stdout, old_hit)
      self.assertEqual(run_ccsi(home, "search", "rebuiltneedle").stdout, "(no hits)\n")

      run_ccsi(home, "index")
      self.assertNotEqual(database.read_bytes(), old_bytes)
      self.assertEqual(run_ccsi(home, "search", "legacyneedle").stdout, old_hit)
      self.assertIn(">>>rebuiltneedle<<<", run_ccsi(home, "search", "rebuiltneedle", "--vendor", "cc").stdout)
      self.assertEqual(run_ccsi(home, "search", "rebuiltneedle", "--exclude-session", "new-session").stdout,
                       "(no hits)\n")

  def test_interrupted_rebuild_preserves_old_db_and_discards_staging_on_retry(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      session = cc_session(home, "before", "interruptneedle")
      run_ccsi(home, "index", script=LEGACY_SCRIPT)
      database = db_path_for(home)
      staging = database.with_suffix(".sqlite.rebuild")
      old_bytes = database.read_bytes()
      old_hit = run_ccsi(home, "search", "interruptneedle", script=LEGACY_SCRIPT).stdout
      fifo = session.with_name("pause.jsonl")
      os.mkfifo(fifo)
      with subprocess.Popen([sys.executable, str(SCRIPT), "index"], env=ccsi_env(home),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE) as rebuild:
        try:
          deadline = time.monotonic() + 5
          while not staging.is_file() and rebuild.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
          self.assertTrue(staging.is_file(), "rebuild did not create a separate index")
          self.assertIsNone(rebuild.poll())
          self.assertEqual(database.read_bytes(), old_bytes)
          self.assertEqual(run_ccsi(home, "search", "interruptneedle").stdout, old_hit)
        finally:
          rebuild.kill()
          rebuild.communicate(timeout=5)
      self.assertEqual(database.read_bytes(), old_bytes)
      self.assertEqual(run_ccsi(home, "search", "interruptneedle").stdout, old_hit)
      self.assertTrue(staging.is_file())
      fifo.unlink()
      cc_session(home, "after", "recoverneedle")
      run_ccsi(home, "index")
      self.assertFalse(staging.exists())
      self.assertIn("after.jsonl", run_ccsi(home, "search", "recoverneedle").stdout)

  def test_extractor_version_change_rebuilds_index(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      cc_session(home, "version", "versionneedle")
      run_ccsi(home, "index")
      database = db_path_for(home)
      with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('extractor_version', 0)")
      old_inode = database.stat().st_ino

      rebuilt = run_ccsi(home, "index")
      self.assertIn("updated=1", rebuilt.stdout)
      self.assertNotEqual(database.stat().st_ino, old_inode)
      self.assertIn(">>>versionneedle<<<", run_ccsi(home, "search", "versionneedle").stdout)


class ChangedSearchSourceTest(unittest.TestCase):
  def test_missing_source_does_not_take_a_result_slot(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      paths = [cc_session(home, name, "sharedneedle") for name in ("first", "second")]
      run_ccsi(home, "index")
      before = run_ccsi(home, "search", "sharedneedle", "-k", "1").stdout
      first, second = sorted(paths, key=lambda path: path.name not in before)
      self.assertIn(first.name, before)
      first.unlink()
      after = run_ccsi(home, "search", "sharedneedle", "-k", "1").stdout
      self.assertIn(second.name, after)
      self.assertNotIn(first.name, after)

  def test_shrunk_or_replaced_source_is_skipped_even_if_the_term_still_matches(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      shrunk = cc_session(home, "shrunk", "shrinkneedle extra words removed later")
      replaced = cc_session(home, "replaced", "replaceneedle initial")
      run_ccsi(home, "index")
      self.assertIn(shrunk.name, run_ccsi(home, "search", "shrinkneedle").stdout)
      self.assertIn(replaced.name, run_ccsi(home, "search", "replaceneedle").stdout)
      cc_session(home, "shrunk", "shrinkneedle")
      replacement = cc_session(home, "replacement", "replaceneedle updated with more words")
      replacement.replace(replaced)
      self.assertEqual(run_ccsi(home, "search", "shrinkneedle").stdout, "(no hits)\n")
      self.assertEqual(run_ccsi(home, "search", "replaceneedle").stdout, "(no hits)\n")

  def test_unreadable_source_does_not_abort_search(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      blocked = cc_session(home, "blocked", "readneedle")
      available = cc_session(home, "available", "readneedle")
      run_ccsi(home, "index")
      self.assertIn(blocked.name, run_ccsi(home, "search", "readneedle").stdout)
      blocked.chmod(0)
      try:
        result = run_ccsi(home, "search", "readneedle")
      finally:
        blocked.chmod(0o600)
      self.assertIn(available.name, result.stdout)
      self.assertNotIn(blocked.name, result.stdout)
      self.assertEqual(result.stderr, "")

  def test_append_keeps_old_hit_and_adds_new_neighbor(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      path = cc_session(home, "append", "stablehit old text")
      run_ccsi(home, "index")
      before = run_ccsi(home, "search", "stablehit", "-C", "1").stdout
      self.assertIn("  > [user 2026-10-19T00:00:00Z] stablehit old text\n", before)
      with path.open("a") as file:
        file.write(json.dumps({"type": "assistant", "timestamp": "2026-10-19T00:00:01Z",
                               "message": {"role": "assistant", "content": "appended neighbor"}}) + "\n")
      after = run_ccsi(home, "search", "stablehit", "-C", "1").stdout
      self.assertEqual(after, before + "  + [assistant 2026-10-19T00:00:01Z] appended neighbor\n")

  def test_short_term_filter_continues_beyond_the_first_candidates(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      for i in range(70):
        cc_session(home, f"filler-{i}", "batchneedle")
      cc_session(home, "matching-a", "batchneedle extra words ZX at end")
      cc_session(home, "matching-b", "batchneedle extra words zx at end")
      run_ccsi(home, "index")
      self.assertIn("filler-", run_ccsi(home, "search", "batchneedle", "-k", "1").stdout)
      first = run_ccsi(home, "search", "batchneedle", "zx", "-k", "1").stdout
      self.assertEqual(first.count("--- [cc]"), 1)
      self.assertIn("matching-", first)
      exhausted = run_ccsi(home, "search", "batchneedle", "zx", "-k", "3").stdout
      self.assertIn("matching-a.jsonl", exhausted)
      self.assertIn("matching-b.jsonl", exhausted)
      self.assertEqual(exhausted.count("--- [cc]"), 2)


class PruneDeletedSessionsTest(unittest.TestCase):
  def test_deleted_session_is_removed_from_index(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      path = cc_session(home, "deleted", "deletedneedle")
      cc_session(home, "retained", "retainedneedle")
      self.assertIn("rows=2 ", run_ccsi(home, "index").stdout)
      self.assertIn(path.name, run_ccsi(home, "search", "deletedneedle").stdout)
      path.unlink()
      self.assertIn("rows=1 ", run_ccsi(home, "index").stdout)
      self.assertEqual(run_ccsi(home, "search", "deletedneedle").stdout, "(no hits)\n")
      self.assertIn("retained.jsonl", run_ccsi(home, "search", "retainedneedle").stdout)

  def test_vendor_scoped_prune_retains_other_vendors(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      cc = cc_session(home, "deleted", "deletedneedle")
      codex = home / ".codex/sessions/probe/rollout.jsonl"
      write_codex_rollout(codex)
      pi = home / ".pi/agent/sessions/probe/pi.jsonl"
      pi.parent.mkdir(parents=True)
      pi.write_text(json.dumps({"type": "message", "message": {"role": "user", "content": "pineedle"}}) + "\n")
      initial = run_ccsi(home, "index").stdout
      total = int(initial.split("rows=")[1].split()[0])
      self.assertIn("rollout.jsonl", run_ccsi(home, "search", "codexneedle").stdout)
      self.assertIn("pi.jsonl", run_ccsi(home, "search", "pineedle").stdout)
      for path in (cc, codex, pi):
        path.rename(path.with_suffix(".backup"))
      self.assertIn(f"rows={total - 1} ", run_ccsi(home, "index", "--vendor", "cc").stdout)
      for path in (codex, pi):
        path.with_suffix(".backup").rename(path)
      self.assertIn("rollout.jsonl", run_ccsi(home, "search", "codexneedle").stdout)
      self.assertIn("pi.jsonl", run_ccsi(home, "search", "pineedle").stdout)
      self.assertEqual(run_ccsi(home, "search", "deletedneedle").stdout, "(no hits)\n")

  def test_missing_root_and_unreadable_file_preserve_index_entries(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      path = cc_session(home, "restore", "restoreneedle")
      self.assertIn("rows=1 ", run_ccsi(home, "index").stdout)
      initial = run_ccsi(home, "search", "restoreneedle").stdout
      self.assertIn(path.name, initial)
      root = home / ".claude/projects"
      relocated = root.rename(root.with_name("projects-away"))
      try:
        self.assertIn("rows=1 ", run_ccsi(home, "index").stdout)
      finally:
        relocated.rename(root)
      self.assertEqual(run_ccsi(home, "search", "restoreneedle").stdout, initial)
      when = path.stat().st_mtime + 5
      os.utime(path, (when, when))
      path.chmod(0)
      try:
        report = run_ccsi(home, "index")
        self.assertIn("failed=1 rows=1 ", report.stdout)
      finally:
        path.chmod(0o600)
      self.assertEqual(run_ccsi(home, "search", "restoreneedle").stdout, initial)


class SearchEquivalenceTest(unittest.TestCase):
  def test_same_corpus_has_identical_legacy_and_contentless_stdout(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      cc = cc_session(home, "equivalence", "共用索引 queryneedle 0% Git settings.json\nsecond line")
      with cc.open("a") as file:
        for content in (
          "<command-name>/review-spec</command-name><command-args>fixed packet</command-args>",
          "<task-notification><summary>oracle task finished</summary><result>expected evidence</result></task-notification>",
          [{"type": "tool_use", "name": "Bash", "input": {"command": "git-closeout"}}],
          [{"type": "tool_use", "name": "Widget", "input": {"options": {"token": "structuredneedle"}}}],
        ):
          file.write(json.dumps({"type": "assistant", "timestamp": "2026-10-19T00:00:01Z",
                                 "message": {"role": "assistant", "content": content}}) + "\n")
      write_codex_rollout(home / ".codex/sessions/probe/rollout.jsonl")
      pi = home / ".pi/agent/sessions/probe/pi.jsonl"
      pi.parent.mkdir(parents=True)
      pi.write_text(json.dumps({"type": "message", "message": {"role": "user", "content": "pineedle 共用索引"}}) + "\n")
      cc_session(home, "tied-one", "rankneedle")
      cc_session(home, "tied-two", "rankneedle")
      old_env = {"CCSI_DB_DIR": str(home / "old-index")}
      run_ccsi(home, "index", script=LEGACY_SCRIPT, extra_env=old_env)
      run_ccsi(home, "index")
      cases = (
        (("共用索引", "-C", "2"), ">>>共用索引<<<"),
        (("settings.json",), ">>>settings.json<<<"),
        (("queryneedle", "0%"), "0%"),
        (("queryneedle", "pi"), "(no hits)\n"),
        (("共用索引", "--vendor", "pi"), "--- [pi]"),
        (("共用索引", "--exclude-session", "equivalence"), "--- [pi]"),
        (("oracle task finished", "-C", "1"), "expected evidence"),
        (("review-spec",), "fixed packet"),
        (("git-closeout",), ">>>git-closeout<<<"),
        (("structuredneedle",), "[Widget]"),
        (("spawn_agent", "--vendor", "codex"), "--- [codex]"),
        (("rankneedle", "-k", "1"), ">>>rankneedle<<<"),
        (("never-indexed",), "(no hits)\n"),
      )
      compared = 0
      for args, expected in cases:
        with self.subTest(args=args):
          old = run_ccsi(home, "search", *args, script=LEGACY_SCRIPT, extra_env=old_env).stdout
          new = run_ccsi(home, "search", *args).stdout
          self.assertIn(expected, old)
          self.assertEqual(new, old)
          compared += 1
      self.assertGreater(compared, 0)
      print(f"\n{compared}/{len(cases)} CLI queries have identical stdout", flush=True)

  def test_only_approved_fixture_changes_differ_between_versions(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      deleted = cc_session(home, "delete", "gonefromdisk")
      appended = cc_session(home, "append", "anchorneedle")
      old_env = {"CCSI_DB_DIR": str(home / "old-index")}
      run_ccsi(home, "index", script=LEGACY_SCRIPT, extra_env=old_env)
      run_ccsi(home, "index")

      def old_search(*args):
        return run_ccsi(home, "search", *args, script=LEGACY_SCRIPT, extra_env=old_env).stdout

      original_context = old_search("anchorneedle", "-C", "1")
      self.assertIn(">>>anchorneedle<<<", original_context)
      self.assertEqual(run_ccsi(home, "search", "anchorneedle", "-C", "1").stdout, original_context)
      self.assertIn(deleted.name, old_search("gonefromdisk"))
      self.assertIn(deleted.name, run_ccsi(home, "search", "gonefromdisk").stdout)
      deleted.unlink()
      with appended.open("a") as file:
        file.write(json.dumps({"type": "assistant", "timestamp": "2026-10-19T00:00:01Z",
                               "message": {"role": "assistant", "content": "new adjacent row"}}) + "\n")
      self.assertEqual(old_search("anchorneedle", "-C", "1"), original_context)
      self.assertEqual(run_ccsi(home, "search", "anchorneedle", "-C", "1").stdout,
                       original_context + "  + [assistant 2026-10-19T00:00:01Z] new adjacent row\n")
      run_ccsi(home, "index", script=LEGACY_SCRIPT, extra_env=old_env)
      run_ccsi(home, "index")
      self.assertIn(deleted.name, old_search("gonefromdisk"))
      self.assertEqual(run_ccsi(home, "search", "gonefromdisk").stdout, "(no hits)\n")


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

      db_path = db_path_for(home)
      with closing(sqlite3.connect(db_path)) as db:
        before_mtime = db.execute(
          "SELECT mtime FROM files JOIN paths USING (path_id) WHERE path = ?",
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
          "SELECT mtime FROM files JOIN paths USING (path_id) WHERE path = ?",
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

      db_path = db_path_for(home)
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
      self.assertIn("--- [codex]", run_ccsi(home, "search", "codexneedle", "--vendor", "codex").stdout)
      self.assertIn("--- [cc]", run_ccsi(home, "search", "ccneedle", "--vendor", "cc").stdout)
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


class SearchQueryTest(unittest.TestCase):
  def test_queries_with_fts_punctuation_match_literally(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      session = home / ".claude/projects/probe/punct.jsonl"
      session.parent.mkdir(parents=True)
      session.write_text(json.dumps({
        "type": "user",
        "sessionId": "punct",
        "timestamp": "2026-09-24T00:00:00Z",
        "message": {"role": "user", "content": "check config.yaml under ~/.cli-proxy-api/logs today"},
      }) + "\n")

      run_ccsi(home, "index")

      for query in ("config.yaml", "~/.cli-proxy-api/logs", "cli-proxy-api"):
        self.assertIn("punct.jsonl", run_ccsi(home, "search", query).stdout, query)


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

      db_path = db_path_for(home)
      with closing(sqlite3.connect(db_path)) as db:
        file_count = db.execute("SELECT count(*) FROM files").fetchone()[0]
        message_count = db.execute("SELECT count(*) FROM messages").fetchone()[0]

      self.assertEqual(file_count, 15_000)
      self.assertEqual(message_count, 15_000)
      self.assertLess(elapsed, 10, f"initial index took {elapsed:.1f}s")


class ObeliskEnhancementsTest(unittest.TestCase):
  def test_append_only_cursor_and_half_line_recovery(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      session = home / ".claude/projects/probe/append.jsonl"
      session.parent.mkdir(parents=True)
      first = json.dumps({"type": "user", "timestamp": "2026-10-19T00:00:00Z", "message": {"role": "user", "content": "firstlinealpha"}}) + "\n"
      half = '{"type": "assistant", "timestamp": "2026-10-19T00:00:01Z", "message": {"role": "assistant", "content": "secondline'
      session.write_text(first + half)
      run_ccsi(home, "index")

      self.assertIn("firstlinealpha", run_ccsi(home, "search", "firstlinealpha").stdout)
      self.assertEqual(run_ccsi(home, "search", "secondlinebeta").stdout.strip(), "(no hits)")

      with open(session, "a") as f:
        f.write('beta"}}\n')
      t = session.stat().st_mtime + 5
      os.utime(session, (t, t))
      run_ccsi(home, "index")

      self.assertIn("firstlinealpha", run_ccsi(home, "search", "firstlinealpha").stdout)
      self.assertIn("secondlinebeta", run_ccsi(home, "search", "secondlinebeta").stdout)
      db_path = db_path_for(home)
      with closing(sqlite3.connect(db_path)) as db:
        total = db.execute("SELECT count(*) FROM messages").fetchone()[0]
      self.assertEqual(total, 2)

  def test_interleaved_context_and_exclude_session(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      s_a = home / ".claude/projects/probe/sess-alpha.jsonl"
      s_b = home / ".claude/projects/probe/sess-bravo.jsonl"
      s_a.parent.mkdir(parents=True)
      s_a.write_text(
        json.dumps({"type": "user", "timestamp": "2026-10-19T00:00:01Z", "message": {"role": "user", "content": "alpha step one"}}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-10-19T00:00:02Z", "message": {"role": "assistant", "content": "alpha step two"}}) + "\n"
      )
      s_b.write_text(
        "".join(
          json.dumps({"type": "user", "timestamp": f"2026-10-19T00:01:{i:02d}Z", "message": {"role": "user", "content": f"bravo filler {i} sharedkeyword"}}) + "\n"
          for i in range(10)
        )
      )
      run_ccsi(home, "index")

      with open(s_a, "a") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-10-19T00:02:00Z", "message": {"role": "user", "content": "alpha targetneedle sharedkeyword"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-19T00:02:01Z", "message": {"role": "assistant", "content": "alpha step four"}}) + "\n")
      t = s_a.stat().st_mtime + 5
      os.utime(s_a, (t, t))
      run_ccsi(home, "index")

      ctx_out = run_ccsi(home, "search", "targetneedle", "-C", "2").stdout
      self.assertIn("alpha step one", ctx_out)
      self.assertIn("alpha step two", ctx_out)
      self.assertIn("alpha step four", ctx_out)
      self.assertNotIn("bravo filler", ctx_out)

      excl_out = run_ccsi(home, "search", "sharedkeyword", "--exclude-session", "sess-bravo").stdout
      self.assertIn("sess-alpha.jsonl", excl_out)
      self.assertNotIn("sess-bravo.jsonl", excl_out)

  def test_tool_summary_and_envelope_normalization(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      s = home / ".claude/projects/probe/tools.jsonl"
      mirror = home / ".claude/projects/codex/mirrored.jsonl"
      s.parent.mkdir(parents=True)
      mirror.parent.mkdir(parents=True)
      mirror.write_text(json.dumps({"type": "user", "timestamp": "2026-10-19T00:00:00Z", "message": {"role": "user", "content": "mirrorneedle"}}) + "\n")
      s.write_text(
        json.dumps({"type": "user", "timestamp": "2026-10-19T00:00:01Z", "message": {"role": "user", "content": "<command-message>github-repo-research</command-message>\n<command-name>/github-repo-research</command-name>\n<command-args>https://github.com/tommy0103/obelisk</command-args>\nBase directory for this skill: /tmp\n# Huge Skill Body shouldbefiltered"}}) + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-10-19T00:00:02Z", "message": {"role": "user", "content": "Base directory for this skill: /tmp\npure skill dump noiseshouldnotmatch"}}) + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-10-19T00:00:03Z", "message": {"role": "user", "content": "<task-notification><task-id>a1</task-id><summary>Agent finished</summary><result>verdictpassneedle</result></task-notification>"}}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-10-19T00:00:04Z", "message": {"role": "assistant", "content": [
          {"type": "tool_use", "id": "u1", "name": "Edit", "input": {"file_path": "/Users/test/advisor.md"}},
          {"type": "tool_use", "id": "u2", "name": "Skill", "input": {"skill": "check-my-stack", "args": "obelisk"}},
          {"type": "tool_use", "id": "u3", "name": "StructuredOutput", "input": {"verdict": "structuredverdictneedle"}},
          {"type": "tool_use", "id": "u4", "name": "TaskList", "input": {}},
        ]}}) + "\n"
      )
      run_ccsi(home, "index")

      self.assertIn("tools.jsonl", run_ccsi(home, "search", "github-repo-research").stdout)
      self.assertIn("tools.jsonl", run_ccsi(home, "search", "tommy0103/obelisk").stdout)
      self.assertEqual(run_ccsi(home, "search", "noiseshouldnotmatch").stdout.strip(), "(no hits)")
      self.assertIn("tools.jsonl", run_ccsi(home, "search", "verdictpassneedle").stdout)
      self.assertIn("tools.jsonl", run_ccsi(home, "search", "advisor.md").stdout)
      self.assertIn("tools.jsonl", run_ccsi(home, "search", "check-my-stack").stdout)
      self.assertIn("tools.jsonl", run_ccsi(home, "search", "structuredverdictneedle").stdout)
      self.assertEqual(run_ccsi(home, "search", "TaskList").stdout.strip(), "(no hits)")
      self.assertEqual(run_ccsi(home, "search", "mirrorneedle").stdout.strip(), "(no hits)")

  def test_pi_vendor_indexing_and_events(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      pi_file = home / ".pi/agent/sessions/--tmp--/2026-10-19_pi1.jsonl"
      pi_file.parent.mkdir(parents=True)
      pi_file.write_text(
        json.dumps({"type": "session", "version": 3, "id": "pi-sess-1", "timestamp": "2026-10-19T01:00:00.000Z", "cwd": "/tmp/piwork"}) + "\n"
        + json.dumps({"type": "model_change", "id": "m1", "timestamp": "2026-10-19T01:00:01.000Z", "provider": "openai", "modelId": "gpt-5.6-luna"}) + "\n"
        + json.dumps({"type": "message", "id": "m2", "timestamp": "2026-10-19T01:00:02.000Z", "message": {"role": "user", "content": [{"type": "text", "text": "pineedle question"}], "timestamp": 1760000002000}}) + "\n"
        + json.dumps({"type": "message", "id": "m3", "timestamp": "2026-10-19T01:00:03.000Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {"file_path": "/tmp/pifile.ts"}}], "timestamp": 1760000003000}}) + "\n"
        + json.dumps({"type": "message", "id": "m4", "timestamp": "2026-10-19T01:00:04.000Z", "message": {"role": "toolResult", "toolName": "read", "content": [{"type": "text", "text": "piresultnotindexed"}], "timestamp": 1760000004000}}) + "\n"
      )
      run_ccsi(home, "index")

      self.assertIn("--- [pi]", run_ccsi(home, "search", "pineedle", "--vendor", "pi").stdout)
      self.assertIn("pifile.ts", run_ccsi(home, "search", "pifile.ts", "--vendor", "pi").stdout)
      self.assertEqual(run_ccsi(home, "search", "piresultnotindexed").stdout.strip(), "(no hits)")
      evs = [json.loads(l) for l in run_ccsi(home, "events", "--vendor", "pi", "--since", "2026-10-19").stdout.splitlines()]
      self.assertEqual([e["kind"] for e in evs], ["session_meta", "text", "tool_call", "tool_result"])
      self.assertEqual(evs[1]["model"], "openai/gpt-5.6-luna")

  def test_short_token_guard_mixed_like_and_context_central_row(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      s = home / ".claude/projects/probe/short-and-ctx.jsonl"
      s.parent.mkdir(parents=True)
      long_tail = "尾端關鍵結論九八七六五"
      filler = "中段填充文字" * 20
      s.write_text(
        json.dumps({"type": "user", "timestamp": "2026-10-19T02:00:01Z", "message": {"role": "user", "content": f"Herdr 左下角 狀態 100% 測試 假綠 {filler} {long_tail}"}}) + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-10-19T02:00:02Z", "message": {"role": "user", "content": "Herdr 左下角 1000 無關內容"}}) + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-10-19T02:00:03Z", "message": {"role": "user", "content": "betaneedle standalone row"}}) + "\n"
      )
      run_ccsi(home, "index")

      pure_short = run_ccsi(home, "search", "白話", check=False)
      self.assertEqual(pure_short.returncode, 2)
      self.assertIn("trigram index requires >=3-char terms", pure_short.stderr)

      two_shorts = run_ccsi(home, "search", "測試", "假綠", check=False)
      self.assertEqual(two_shorts.returncode, 2)

      bool_short = run_ccsi(home, "search", "Herdr AND 狀態", check=False)
      self.assertEqual(bool_short.returncode, 2)

      quoted_short = run_ccsi(home, "search", 'Herdr "狀態"', check=False)
      self.assertEqual(quoted_short.returncode, 2)

      quoted_phrase = run_ccsi(home, "search", '"測試 假綠"')
      self.assertIn("short-and-ctx.jsonl", quoted_phrase.stdout)

      mixed_hit = run_ccsi(home, "search", "Herdr", "左下角", "狀態")
      self.assertEqual(mixed_hit.stdout.count("--- [cc]"), 1)

      mixed_miss = run_ccsi(home, "search", "Herdr", "左下角", "亂講")
      self.assertEqual(mixed_miss.stdout.strip(), "(no hits)")

      escaped_pct = run_ccsi(home, "search", "Herdr", "左下角", "0%")
      self.assertEqual(escaped_pct.stdout.count("--- [cc]"), 1)

      or_query = run_ccsi(home, "search", "左下角 OR betaneedle")
      self.assertEqual(or_query.stdout.count("--- [cc]"), 3)

      punct_or = run_ccsi(home, "search", "100% OR betaneedle")
      self.assertEqual(punct_or.stdout.count("--- [cc]"), 2)

      ctx_out = run_ccsi(home, "search", "Herdr 左下角 狀態", "-C", "1").stdout
      self.assertIn(long_tail, ctx_out)

      db_path = db_path_for(home)
      self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
      self.assertEqual(db_path.parent.stat().st_mode & 0o777, 0o700)

  def test_custom_env_vars_override_roots_and_db_dir(self):
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp)
      custom_cc = home / "custom-claude"
      custom_db = home / "custom-cache/ccsi-db"
      s = custom_cc / "projects/probe/envtest.jsonl"
      s.parent.mkdir(parents=True)
      s.write_text(
        json.dumps({"type": "user", "timestamp": "2026-10-19T03:00:00Z", "message": {"role": "user", "content": "customenvneedle"}}) + "\n"
      )
      extra = {"CLAUDE_CONFIG_DIR": str(custom_cc), "CCSI_DB_DIR": str(custom_db)}
      run_ccsi(home, "index", extra_env=extra)
      self.assertTrue((custom_db / "index.sqlite").exists())
      out = run_ccsi(home, "search", "customenvneedle", extra_env=extra).stdout
      self.assertIn("envtest.jsonl", out)


if __name__ == "__main__":
  unittest.main()
