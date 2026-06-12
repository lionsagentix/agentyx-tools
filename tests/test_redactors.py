# Copyright 2026 Agentyx
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""Round-trip, leak and CLI tests for the Agentyx redactors.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)

import agentyx_redact_cobol as cob              # noqa: E402
import agentyx_redact_zig as zig                # noqa: E402


class CobolCore(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIXTURES, "sample.cbl")) as f:
            self.src = f.read()
        self.state = cob.new_state()
        self.red = cob.redact_text(self.src, self.state)
        self.rev = self.state[1]

    def test_round_trip_lossless(self):
        self.assertEqual(cob.rehydrate_text(self.red, self.rev), self.src)

    def test_no_leaks(self):
        self.assertEqual(cob.leak_check(self.red, self.rev), [])

    def test_names_and_literals_hidden(self):
        for secret in ("WS-FEE-RATE", "WS-CUSTOMER-NAME", "PAYCALC",
                       "'ACME HOLDINGS'", "MAIN-PARA", "1250.75", "2.50"):
            self.assertNotIn(secret, self.red, secret)

    def test_comment_text_hidden(self):
        self.assertNotIn("BASIS POINTS OVER PRIME", self.red)
        # the number inside the comment must not survive the level-number
        # heuristic (the leak the public tool fixes over the prototype)
        for line in self.red.splitlines():
            if cob._is_comment_line(line):
                self.assertNotIn("250", line)

    def test_structure_kept(self):
        for kept in ("PIC 9(2)V99", "PIC X(30)", "OCCURS 5 TIMES", "COMP-3",
                     "PROCEDURE DIVISION", "PERFORM", "GOBACK"):
            self.assertIn(kept, self.red, kept)
        self.assertIn("01  DN_", self.red)        # level numbers kept

    def test_consistent_tokens_across_files(self):
        state = cob.new_state()
        a = cob.redact_text("MOVE WS-X TO WS-Y.", state)
        b = cob.redact_text("ADD 1 TO WS-X.", state)
        tok = a.split()[1]
        self.assertIn(tok, b)

    def test_collision_token_roundtrip_guarded(self):
        # redact() CLI refuses input that already contains token patterns;
        # core check: the regex used by the guard matches them.
        self.assertTrue(cob.TOKEN_RE.search("MOVE DN_3 TO X."))


class ZigCore(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIXTURES, "sample.zig")) as f:
            self.src = f.read()
        self.state = zig.new_state()
        self.red = zig.redact_text(self.src, self.state)
        self.rev = self.state[1]

    def test_round_trip_lossless(self):
        self.assertEqual(zig.rehydrate_text(self.red, self.rev), self.src)

    def test_names_strings_comments_hidden(self):
        for secret in ("monthlyFee", "fee_table", "basisPoints", "tierName",
                       '"platinum"', '"gold"', "Acme Holdings",
                       "proprietary fee logic", "VIP customers"):
            self.assertNotIn(secret, self.red, secret)

    def test_no_leaks(self):
        self.assertEqual(zig.leak_check(self.red, self.rev), [])

    def test_structure_kept(self):
        for kept in ('@import("std")', "pub fn", "[]const u8", "u64",
                     "std.mem.eql", "[_]", "10_000", "comptime"):
            if kept == "comptime":
                continue                          # not in this fixture
            self.assertIn(kept, self.red, kept)

    def test_comment_markers_kept(self):
        self.assertIn("//!CMT_", self.red)
        self.assertIn("///CMT_", self.red)
        self.assertIn("//CMT_", self.red)

    def test_multiline_string_redacted(self):
        self.assertIn("\\\\MLS_", self.red)
        self.assertNotIn("do not distribute", self.red)

    def test_lone_underscore_kept(self):
        self.assertIn("[_]", self.red)


class CliEndToEnd(unittest.TestCase):
    """Drive each tool's CLI exactly as a customer would."""

    def _run(self, script, *argv):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, script), *argv],
            capture_output=True, text=True)

    def _flow(self, script, fixture, exts):
        work = tempfile.mkdtemp()
        try:
            src_dir = os.path.join(work, "src")
            os.makedirs(src_dir)
            shutil.copy(os.path.join(FIXTURES, fixture), src_dir)
            red = os.path.join(work, "redacted")
            keys = os.path.join(work, "agentyx.keys.json")

            r = self._run(script, "redact", "--in", src_dir, "--out", red,
                          "--keys", keys)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("round-trip lossless: 1/1", r.stdout)
            self.assertIn("CLEAN", r.stdout)

            v = self._run(script, "verify", "--original", src_dir,
                          "--redacted", red, "--keys", keys)
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
            self.assertIn("verify PASSED", v.stdout)

            final = os.path.join(work, "final")
            h = self._run(script, "rehydrate", "--in", red, "--out", final,
                          "--keys", keys)
            self.assertEqual(h.returncode, 0, h.stdout + h.stderr)
            with open(os.path.join(FIXTURES, fixture)) as f:
                orig = f.read()
            with open(os.path.join(final, fixture)) as f:
                self.assertEqual(f.read(), orig)

            with open(keys) as f:
                meta = json.load(f)
            self.assertEqual(meta["format"], "agentyx-keys/1")
            self.assertIn("WARNING", meta)

            # refuses to clobber an existing keys file
            r2 = self._run(script, "redact", "--in", src_dir, "--out", red,
                           "--keys", keys)
            self.assertNotEqual(r2.returncode, 0)

            # refuses already-redacted input (collision guard)
            r3 = self._run(script, "redact", "--in", red, "--out",
                           os.path.join(work, "red2"),
                           "--keys", os.path.join(work, "k2.json"),
                           "--ext", exts)
            self.assertNotEqual(r3.returncode, 0)
            self.assertIn("already contains", r3.stderr + r3.stdout)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_cobol_cli(self):
        self._flow("agentyx_redact_cobol.py", "sample.cbl", "cbl")

    def test_zig_cli(self):
        self._flow("agentyx_redact_zig.py", "sample.zig", "zig")


if __name__ == "__main__":
    unittest.main()
