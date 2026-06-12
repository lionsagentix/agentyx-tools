#!/usr/bin/env python3
# Copyright 2026 Agentyx
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Agentyx COBOL redactor — run this on YOUR machine, before anything is uploaded.

What it does
------------
Replaces everything in your COBOL source that carries meaning with neutral
tokens, while keeping the structure the Agentyx conversion pipeline needs:

  KEPT (visible after redaction)         REDACTED (replaced by tokens)
  --------------------------------       -------------------------------------
  COBOL reserved words                   data names / paragraph names  -> DN_n
  PIC/PICTURE clauses                    string + hex literals         -> STR_n
  level numbers, OCCURS counts           value & computation numbers   -> NUM_n
  punctuation, layout, columns           comment text                  -> CMT_n

The token map is written to a local keys file. THE KEYS FILE NEVER LEAVES YOUR
MACHINE — it is the only thing that can turn tokens back into your real names,
values and comments. You upload ONLY the redacted files; when Agentyx returns
results, you run `rehydrate` locally with your keys file to restore them.

This script is self-contained, uses only the Python 3 standard library, and
makes NO network connections of any kind. Audit it: it is one file.

Usage
-----
  python3 agentyx_redact_cobol.py redact    --in ./src --out ./redacted --keys ./agentyx.keys.json
  python3 agentyx_redact_cobol.py verify    --original ./src --redacted ./redacted --keys ./agentyx.keys.json
  python3 agentyx_redact_cobol.py rehydrate --in ./results --out ./final --keys ./agentyx.keys.json

`redact` also self-verifies: it rehydrates its own output in memory and checks
it is byte-identical to your original, and it scans the redacted output to
confirm no redacted name or literal still appears in it.

Honest limits (read this)
-------------------------
- The SHAPE of your program (control flow, record layouts, PIC clauses, level
  numbers) remains visible — that is exactly what conversion consumes.
- Level numbers, OCCURS counts and sequence-number columns are kept (they are
  structure). All other numeric literals are redacted.
- Files must be UTF-8 (or ASCII) text. The tool warns if it meets bytes it
  cannot decode losslessly.
"""
import argparse
import json
import os
import re
import sys

TOOL = "agentyx_redact_cobol.py"
VERSION = "0.1.0"
KEYS_FORMAT = "agentyx-keys/1"
LANGUAGE = "cobol"
DEFAULT_EXTS = (".cbl", ".cob", ".cpy", ".cobol")
TOKEN_KINDS = ("DN", "STR", "NUM", "CMT")
TOKEN_RE = re.compile(r"\b(?:DN|STR|NUM|CMT)_\d+\b")

KEYS_WARNING = (
    "PRIVATE KEY MAP — never upload or share this file. It is the only thing "
    "that can turn the redacted tokens back into your real names, values and "
    "comments. Keep it with your source code, under your normal source-code "
    "controls. If you lose it, re-running this same tool version on the "
    "unchanged original source regenerates the identical keys; without "
    "either, redacted results cannot be restored."
)

# Conservative COBOL reserved-word set: only words we are SURE are syntax stay
# visible. A word this list misses is over-redacted (privacy-safe), never leaked.
KEYWORDS = {w.lower() for w in """
IDENTIFICATION DIVISION PROGRAM-ID AUTHOR DATE-WRITTEN ENVIRONMENT CONFIGURATION
SOURCE-COMPUTER OBJECT-COMPUTER SPECIAL-NAMES INPUT-OUTPUT SECTION FILE-CONTROL
SELECT ASSIGN ORGANIZATION ACCESS MODE SEQUENTIAL RANDOM DYNAMIC RELATIVE INDEXED
STATUS DATA FILE FD SD WORKING-STORAGE LINKAGE LOCAL-STORAGE SCREEN REPORT LABEL
RECORD RECORDS STANDARD OMITTED BLOCK CONTAINS COPY IN OF IS ARE THE
PIC PICTURE VALUE VALUES OCCURS TIMES DEPENDING REDEFINES RENAMES FILLER USAGE
COMP COMP-1 COMP-2 COMP-3 COMP-4 COMP-5 COMP-6 BINARY PACKED-DECIMAL DISPLAY
DISPLAY-1 SIGN LEADING TRAILING SEPARATE CHARACTER JUSTIFIED JUST RIGHT LEFT
BLANK WHEN ZERO ZEROS ZEROES SPACE SPACES HIGH-VALUE HIGH-VALUES LOW-VALUE
LOW-VALUES QUOTE QUOTES NULL NULLS ALL KEY ASCENDING DESCENDING SYNCHRONIZED SYNC
PROCEDURE USING RETURNING GIVING MOVE TO FROM ADD SUBTRACT MULTIPLY DIVIDE COMPUTE
ROUNDED REMAINDER INTO BY IF THEN ELSE END-IF EVALUATE ALSO END-EVALUATE PERFORM
UNTIL VARYING AFTER BEFORE THRU THROUGH WITH TEST END-PERFORM GO GOBACK STOP RUN
EXIT PROGRAM PARAGRAPH CONTINUE NEXT SENTENCE ACCEPT OPEN CLOSE READ WRITE REWRITE
DELETE START INPUT OUTPUT I-O EXTEND AT END INVALID NOT NEXT-RECORD UPON LINE LINES
PAGE STRING UNSTRING INSPECT REPLACING TALLYING CONVERTING DELIMITED DELIMITER SIZE
COUNT POINTER OVERFLOW CALL CANCEL SET UP DOWN SEARCH INITIALIZE AND OR EQUAL
GREATER LESS THAN EQUALS NUMERIC ALPHABETIC ALPHABETIC-LOWER ALPHABETIC-UPPER
POSITIVE NEGATIVE TRUE FALSE COL COLUMN ERASE EOS EOL REVERSE-VIDEO BLINK HIGHLIGHT
LOWLIGHT UNDERLINE FOREGROUND-COLOR BACKGROUND-COLOR BELL AUTO SECURE REQUIRED
FULL PROMPT UPDATE RETURN FUNCTION REFERENCE CONTENT LENGTH ADDRESS CORRESPONDING
CORR TIME DATE DAY YEAR CENTURY NO MODULES POSITION MILLENNIUM
""".split()}

_PIC = r"[-Xx9SsVvAaPpZz*$()CcRrDdBb0-9+.,/]+"
_TOK = re.compile(r"""
    (?P<ws>[ \t]+)
  | (?P<cmt>\*>[^\n]*)
  | (?P<pic>(?i:PIC(?:TURE)?)\s+""" + _PIC + r""")
  | (?P<hex>(?i:[XGNZ])'[^']*')
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<token>[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)
  | (?P<other>.)
""", re.X)

_PURE_NUM = re.compile(r"\d+(?:\.\d+)?$")


def new_state():
    """One shared map for a whole tree: the same name gets the same token in
    every program and copybook (the converter needs that consistency)."""
    return {}, {}, {k: 0 for k in TOKEN_KINDS}


def _is_comment_line(line):
    """Fixed-format comment: '*' or '/' in column 7, sequence area blank/digits."""
    return (len(line) > 6 and line[6] in "*/"
            and all(c.isdigit() or c == " " for c in line[:6]))


def redact_text(text, state):
    fwd, rev, cnt = state

    def tok(kind, lexeme):
        key = (kind, lexeme)
        if key in fwd:
            return fwd[key]
        cnt[kind] += 1
        t = f"{kind}_{cnt[kind]}"
        fwd[key] = t
        rev[t] = lexeme
        return t

    out_lines = []
    for line in text.split("\n"):
        if _is_comment_line(line):
            body = line[7:]
            out_lines.append(line[:7] + (tok("CMT", body) if body else ""))
            continue
        res, prev, at_start = [], None, True
        for m in _TOK.finditer(line):
            k, v = m.lastgroup, m.group()
            if k in ("ws", "other"):
                res.append(v)
                continue
            if k == "cmt":                       # inline `*>` comment
                res.append("*>" + tok("CMT", v[2:]) if len(v) > 2 else v)
                prev = None
                at_start = False
                continue
            if k == "pic":                       # PIC clause -> structural, keep whole
                res.append(v)
                prev = None
                at_start = False
                continue
            if k in ("str", "hex"):              # literals -> sensitive, redact
                res.append(tok("STR", v))
                prev = None
                at_start = False
                continue
            # k == "token"
            if _PURE_NUM.match(v):
                if at_start or prev == "OCCURS":  # level number / array size -> keep
                    res.append(v)
                else:                             # value / computation literal -> redact
                    res.append(tok("NUM", v))
                prev = None
            else:
                if v.lower() in KEYWORDS:
                    res.append(v)
                    prev = v.upper()
                else:                             # data / paragraph name -> redact
                    res.append(tok("DN", v))
                    prev = None
            at_start = False
        out_lines.append("".join(res))
    return "\n".join(out_lines)


def rehydrate_text(text, rev):
    return TOKEN_RE.sub(lambda m: rev.get(m.group(0), m.group(0)), text)


def leak_check(redacted, rev):
    """Confirm no redacted name/literal still appears in the redacted text."""
    leaks = []
    for t, lex in rev.items():
        if t.startswith("DN_"):
            if re.search(r"(?<![A-Za-z0-9-])" + re.escape(lex) + r"(?![A-Za-z0-9-])",
                         redacted):
                leaks.append(lex)
        elif t.startswith("STR_") and lex in redacted:
            leaks.append(lex)
    return leaks


# ───────────────────────────── file plumbing ─────────────────────────────

def _collect(path, exts):
    if os.path.isfile(path):
        return [path]
    found = []
    # deterministic traversal (dirs AND files sorted): redacting the same tree
    # twice yields the SAME tokens — a lost keys file is recoverable by re-run.
    for r, dirs, files in os.walk(path):
        dirs.sort()
        for fn in sorted(files):
            if fn.lower().endswith(exts):
                found.append(os.path.join(r, fn))
    return found


def _read(path):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    return text, ("�" in text)


def _out_path(in_root, src, out_root):
    rel = os.path.relpath(src, in_root) if not os.path.isfile(in_root) \
        else os.path.basename(src)
    dst = os.path.join(out_root, rel)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    return dst


def _load_keys(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "map" in data:
        return data["map"]
    return data                                   # bare map, accepted


# ───────────────────────────── commands ─────────────────────────────

def cmd_redact(args):
    exts = tuple(e if e.startswith(".") else "." + e
                 for e in args.ext.lower().split(","))
    files = _collect(args.input, exts)
    if not files:
        sys.exit(f"error: no {'/'.join(exts)} files found under {args.input}")
    if os.path.exists(args.keys) and not args.force:
        sys.exit(f"error: keys file {args.keys} already exists — choose another "
                 f"path or pass --force to overwrite it")

    state = new_state()
    fwd, rev, cnt = state
    originals, problems = {}, []
    for src in files:
        text, had_bad_bytes = _read(src)
        if had_bad_bytes:
            problems.append(f"{src}: contains bytes that are not valid UTF-8 "
                            f"(replaced); round-trip is checked against the "
                            f"decoded text")
        if TOKEN_RE.search(text):
            sys.exit(f"error: {src} already contains DN_n/STR_n/NUM_n/CMT_n "
                     f"tokens — redacting it would corrupt rehydration. "
                     f"Is this file already redacted?")
        originals[src] = text

    n_lossless = 0
    for src in files:
        red = redact_text(originals[src], state)
        ok = rehydrate_text(red, rev) == originals[src]
        n_lossless += ok
        if not ok:
            problems.append(f"{src}: round-trip NOT lossless — do not upload; "
                            f"please report this file's structure to Agentyx")
        with open(_out_path(args.input, src, args.out), "w",
                  encoding="utf-8") as f:
            f.write(red)

    all_red = "\n".join(open(_out_path(args.input, s, args.out),
                             encoding="utf-8").read() for s in files)
    leaks = leak_check(all_red, rev)

    keys = {
        "format": KEYS_FORMAT,
        "language": LANGUAGE,
        "tool": f"{TOOL} v{VERSION}",
        "WARNING": KEYS_WARNING,
        "counts": dict(cnt),
        "map": rev,
    }
    with open(args.keys, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=1)

    print(f"redacted {len(files)} file(s) -> {args.out}")
    print(f"  hidden: {cnt['DN']} names, {cnt['STR']} string/hex literals, "
          f"{cnt['NUM']} numeric values, {cnt['CMT']} comments")
    print(f"  kept visible (structure): reserved words, PIC clauses, level "
          f"numbers, OCCURS counts, layout")
    print(f"  round-trip lossless: {n_lossless}/{len(files)} files")
    print(f"  leak scan over redacted output: "
          f"{'CLEAN — no redacted name or literal appears' if not leaks else 'LEAKS: ' + ', '.join(leaks[:5])}")
    print(f"  keys written to {args.keys} — KEEP THIS FILE PRIVATE; never "
          f"upload it")
    for p in problems:
        print(f"  warning: {p}")
    if leaks or n_lossless != len(files):
        sys.exit(1)


def cmd_verify(args):
    rev = _load_keys(args.keys)
    exts = tuple(e if e.startswith(".") else "." + e
                 for e in args.ext.lower().split(","))
    files = _collect(args.original, exts)
    bad = 0
    for src in files:
        orig, _ = _read(src)
        red_path = _out_path(args.original, src, args.redacted)
        if not os.path.exists(red_path):
            print(f"  MISSING  {red_path}")
            bad += 1
            continue
        red, _ = _read(red_path)
        ok = rehydrate_text(red, rev) == orig
        leaks = leak_check(red, rev)
        status = "OK" if ok and not leaks else ("MISMATCH" if not ok else "LEAK")
        bad += status != "OK"
        print(f"  {status:8} {os.path.relpath(src, args.original) if not os.path.isfile(args.original) else src}")
    print(("verify PASSED: every redacted file rehydrates to your original, "
           "byte for byte, and leaks nothing") if not bad else
          f"verify FAILED on {bad} file(s) — do not upload")
    sys.exit(1 if bad else 0)


def cmd_rehydrate(args):
    rev = _load_keys(args.keys)
    files = ([args.input] if os.path.isfile(args.input)
             else [os.path.join(r, fn)
                   for r, _, fs in os.walk(args.input) for fn in sorted(fs)])
    n = 0
    for src in files:
        text, _ = _read(src)
        restored = rehydrate_text(text, rev)
        with open(_out_path(args.input, src, args.out), "w",
                  encoding="utf-8") as f:
            f.write(restored)
        n += 1
    print(f"rehydrated {n} file(s) -> {args.out}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL,
        description="Agentyx COBOL redactor — redact locally, upload only "
                    "tokens, keep the keys.")
    ap.add_argument("--version", action="version", version=f"{TOOL} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("redact", help="redact a file or tree; write keys file")
    p.add_argument("--in", dest="input", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keys", required=True)
    p.add_argument("--ext", default=",".join(e[1:] for e in DEFAULT_EXTS),
                   help="comma-separated extensions (default: cbl,cob,cpy,cobol)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing keys file")
    p.set_defaults(fn=cmd_redact)

    p = sub.add_parser("verify",
                       help="independently confirm redacted == lossless + leak-free")
    p.add_argument("--original", required=True)
    p.add_argument("--redacted", required=True)
    p.add_argument("--keys", required=True)
    p.add_argument("--ext", default=",".join(e[1:] for e in DEFAULT_EXTS))
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("rehydrate",
                       help="restore real names in files using your local keys")
    p.add_argument("--in", dest="input", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keys", required=True)
    p.set_defaults(fn=cmd_rehydrate)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
