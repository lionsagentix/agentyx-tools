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
"""Agentyx Zig redactor — run this on YOUR machine, before anything is uploaded.

What it does
------------
Replaces everything in your Zig source that carries meaning with neutral
tokens, while keeping the structure the Agentyx conversion pipeline needs:

  KEPT (visible after redaction)            REDACTED (replaced by tokens)
  ------------------------------------      ----------------------------------
  Zig keywords, @builtins, primitive        your identifiers           -> ID_n
    types, std-library API names            string literals            -> STR_n
  numbers, char literals (structural:       multiline \\\\ string lines  -> MLS_n
    array sizes, comptime values)           comment text               -> CMT_n
  @import("...") paths (module wiring)
  the lone `_` (array-size inference)

The token map is written to a local keys file. THE KEYS FILE NEVER LEAVES YOUR
MACHINE — it is the only thing that can turn tokens back into your real names,
strings and comments. You upload ONLY the redacted files; when Agentyx returns
results, you run `rehydrate` locally with your keys file to restore them.

This script is self-contained, uses only the Python 3 standard library, and
makes NO network connections of any kind. Audit it: it is one file.

Usage
-----
  python3 agentyx_redact_zig.py redact    --in ./src --out ./redacted --keys ./agentyx.keys.json
  python3 agentyx_redact_zig.py verify    --original ./src --redacted ./redacted --keys ./agentyx.keys.json
  python3 agentyx_redact_zig.py rehydrate --in ./results --out ./final --keys ./agentyx.keys.json

`redact` also self-verifies: it rehydrates its own output in memory and checks
it is byte-identical to your original, and it scans the redacted output to
confirm no redacted identifier or string still appears in it.

Honest limits (read this)
-------------------------
- The SHAPE of your program (control flow, types, call graph) remains visible —
  that is exactly what conversion consumes.
- Numeric and character literals are kept: in Zig they are routinely structural
  (array sizes, comptime arithmetic) and replacing them would change what the
  code means. If specific numbers are themselves secrets, tell us before an
  engagement.
- Identifiers that happen to spell a Zig keyword, primitive type or std-library
  API name stay visible (they are indistinguishable from the public vocabulary).
- Files must be UTF-8 text. The tool warns if it meets bytes it cannot decode
  losslessly.
"""
import argparse
import json
import os
import re
import sys

TOOL = "agentyx_redact_zig.py"
VERSION = "0.1.0"
KEYS_FORMAT = "agentyx-keys/1"
LANGUAGE = "zig"
DEFAULT_EXTS = (".zig",)
TOKEN_KINDS = ("ID", "STR", "MLS", "CMT")
TOKEN_RE = re.compile(r"\b(?:ID|STR|MLS|CMT)_\d+\b")

KEYS_WARNING = (
    "PRIVATE KEY MAP — never upload or share this file. It is the only thing "
    "that can turn the redacted tokens back into your real names, strings and "
    "comments. Keep it with your source code, under your normal source-code "
    "controls. If you lose it, re-running this same tool version on the "
    "unchanged original source regenerates the identical keys; without "
    "either, redacted results cannot be restored."
)

# Zig language core: keywords, primitive types, public std-library vocabulary.
# These are the public language surface — keeping them visible leaks nothing of
# yours, and the converter needs them to recognize the constructs.
KEEP = {w.lower() for w in """
fn const var comptime errdefer defer try catch async await suspend nosuspend
unreachable struct enum union opaque pub return if else while for switch break
continue and or orelse error test usingnamespace export extern inline noalias
align packed volatile allowzero threadlocal linksection callconv anyframe
anytype anyerror void bool type noreturn null undefined true false
addrspace asm
u8 u16 u32 u64 u128 usize i8 i16 i32 i64 i128 isize f16 f32 f64 f80 f128
c_char c_short c_int c_long c_longlong c_uint c_ulong c_ulonglong anyopaque
std Allocator ArrayList ArrayListUnmanaged AutoHashMap StringHashMap mem math
fmt debug assert meta Type log heap testing ascii unicode
alloc free create destroy deinit init append appendSlice appendAssumeCapacity
items len ptr self Self this it i j k n
builtin root c
eql eqlComptime order indexOf lastIndexOf startsWith endsWith trim split
tokenize copy set sort min max clamp
expect expectEqual expectEqualSlices expectEqualStrings expectError
""".split()}

_ZTOK = re.compile(r"""
    (?P<ws>[ \t\r\n]+)
  | (?P<comment>//[^\n]*)
  | (?P<mlstr>\\\\[^\n]*)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<char>'(?:\\.|[^'\\])*')
  | (?P<builtin>@[A-Za-z_][A-Za-z0-9_]*)
  | (?P<num>0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eEpP][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<other>.)
""", re.X | re.S)

_CMT_MARKER = re.compile(r"^//[!/]?")


def new_state():
    """One shared map for a whole tree: the same name gets the same token in
    every file (the converter needs that consistency)."""
    return {}, {}, {k: 0 for k in TOKEN_KINDS}


def redact_text(text, state):
    fwd, rev, cnt = state

    def tok(kind, lexeme):
        key = (kind, lexeme)          # key on KIND too: a string "none" (STR)
        if key in fwd:                # and a comment "none" (CMT) must not collide
            return fwd[key]
        cnt[kind] += 1
        t = f"{kind}_{cnt[kind]}"
        fwd[key] = t
        rev[t] = lexeme
        return t

    out = []
    keep_next_str = False
    for m in _ZTOK.finditer(text):
        k, v = m.lastgroup, m.group()
        if k in ("ws", "num", "other"):
            out.append(v)                       # structure — keep
        elif k == "comment":
            # keep the marker (//, ///, //!) so it still parses as the same
            # kind of comment; redact the text after it.
            marker = _CMT_MARKER.match(v).group()
            body = v[len(marker):]
            out.append(marker + (tok("CMT", body) if body else ""))
        elif k == "mlstr":                      # Zig multiline line-string `\\…`
            out.append(v[:2] + tok("MLS", v[2:]))
        elif k == "builtin":
            out.append(v)
            keep_next_str = v in ("@import", "@cImport", "@embedFile")
        elif k == "char":
            out.append(v)                       # char literals = codepoints — keep
        elif k == "str":
            if keep_next_str:                   # @import path — module resolution
                out.append(v)
                keep_next_str = False
            else:
                out.append('"' + tok("STR", v) + '"')
        else:                                   # ident
            # lone `_` is Zig STRUCTURAL syntax (array-size inference `[_]T`,
            # discard binding) — never a proprietary name; redacting it breaks
            # const-array recognition. uN/iN are integer types.
            keep = (v == "_" or v.lower() in KEEP
                    or re.fullmatch(r"[ui]\d+", v))
            out.append(v if keep else tok("ID", v))
            keep_next_str = False
    return "".join(out)


def rehydrate_text(text, rev):
    text = re.sub(r"MLS_\d+", lambda m: rev.get(m.group(0), m.group(0)), text)
    text = re.sub(r'"(STR_\d+)"', lambda m: rev.get(m.group(1), m.group(0)), text)
    return re.sub(r"\b(?:ID|CMT)_\d+\b",
                  lambda m: rev.get(m.group(0), m.group(0)), text)


# ───────────── result rehydration for EMITTED RUST (--target rust) ─────────────
# The Agentyx Zig→Rust emitter (v1.10.x) applies deterministic NAME-DEPENDENT
# transforms while generating Rust. Restoring tokens verbatim would skip them,
# so this faithful inverse restores each token AND re-applies the transform the
# emitter would have applied to the real name:
#   - camelCase VALUE names -> snake_case (types/PascalCase untouched)
#   - Rust-keyword collisions -> r#keyword escape
#   - Zig \xNN escape runs -> \u{..} normalization inside restored strings
#   - a `#[allow(non_snake_case)]` emitted for a tokenized name is dropped when
#     the restored name needs no exemption
# Result: rehydrate_rust(emit(redact(x))) == emit(x), byte for byte.

_RUST_KEYWORDS = frozenset((
    "as", "break", "const", "continue", "dyn", "else", "enum", "extern",
    "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
    "move", "mut", "pub", "ref", "return", "static", "struct", "trait",
    "true", "type", "unsafe", "use", "where", "while", "async", "await",
    "abstract", "become", "box", "do", "final", "macro", "override", "priv",
    "typeof", "unsized", "virtual", "yield", "try", "gen",
))


def _to_snake(name):
    t = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    t = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", t)
    return t.lower()


def _is_camel_value(name):
    return bool(re.fullmatch(r"[a-z][A-Za-z0-9]*", name or "")) and any(
        c.isupper() for c in name)


def _lower_zig_str_escapes(s):
    def _repl(m):
        run = m.group(0)
        bs = bytes(int(h, 16) for h in re.findall(r"\\x([0-9A-Fa-f]{2})", run))
        if all(b <= 0x7F for b in bs):
            return run
        return "".join("\\u{%x}" % ord(c) for c in bs.decode("utf-8", "replace"))
    return re.sub(r"(?:\\x[0-9A-Fa-f]{2})+", _repl, s)


def rehydrate_rust_text(text, rev):
    """Faithful inverse for Rust (and docs) EMITTED by Agentyx from redacted
    Zig — token restore + the emitter's name-dependent transforms."""
    # strings: restore original content, apply the emitter's escape lowering
    def _str(m):
        full = rev.get(m.group(1))
        if full is None:
            return m.group(0)
        inner = full[1:-1] if len(full) >= 2 and full[0] in "\"'" else full
        return '"' + _lower_zig_str_escapes(inner) + '"'
    text = re.sub(r'"(STR_\d+)"', _str, text)
    # bare STR token (string used unquoted, e.g. a generic-type position)
    text = re.sub(r"\bSTR_\d+\b",
                  lambda m: rev[m.group(0)][1:-1] if m.group(0) in rev
                  and len(rev[m.group(0)]) >= 2 else m.group(0), text)
    text = re.sub(r"MLS_\d+", lambda m: rev.get(m.group(0), m.group(0)), text)
    text = re.sub(r"\bCMT_\d+\b", lambda m: rev.get(m.group(0), m.group(0)), text)

    # identifiers: restore, mirror snake_case normalization, r#-escape keywords
    def _restore_id(m):
        real = rev.get(m.group(0), m.group(0))
        if _is_camel_value(real):
            real = _to_snake(real)
        return ("r#" + real) if real in _RUST_KEYWORDS else real
    text = re.sub(r"ID_\d+", _restore_id, text)
    # the emitter case-folds some tokens (test names) — mirror to lowercase
    text = re.sub(r"(?<![A-Za-z])id_(\d+)",
                  lambda m: rev.get("ID_" + m.group(1), m.group(0)).lower()
                  if "ID_" + m.group(1) in rev else m.group(0), text)

    # drop a `#[allow(non_snake_case)]` whose (now-real) decl name needs none
    decl = re.compile(r"\b(?:fn|struct|impl|enum|union|type)\s+([A-Za-z_]\w*)")
    lines, out = text.split("\n"), []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("#[allow(") and "non_snake_case" in s \
                and not s.startswith("#!"):
            m = decl.search(lines[i + 1] if i + 1 < len(lines) else "")
            if m and not any(c.isupper() for c in m.group(1)):
                continue
        out.append(ln)
    return "\n".join(out)


def leak_check(redacted, rev):
    """Confirm no redacted identifier/string/comment still appears.

    Surfaces the policy deliberately KEEPS — @import("...") module paths and
    char literals — are masked out first: they are disclosed as kept in the
    report, so a lexeme inside them is not a leak of a redacted token."""
    redacted = re.sub(
        r'@(?:import|cImport|embedFile)\s*\(\s*"(?:\\.|[^"\\])*"',
        "@import(\"\"", redacted)
    redacted = re.sub(r"'(?:\\.|[^'\\])*'", "''", redacted)
    leaks = []
    for t, lex in rev.items():
        if t.startswith("ID_"):
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(lex) + r"(?![A-Za-z0-9_])",
                         redacted):
                leaks.append(lex)
        elif t.startswith(("STR_", "MLS_", "CMT_")):
            if len(lex) > 2 and lex in redacted:
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
            sys.exit(f"error: {src} already contains ID_n/STR_n/MLS_n/CMT_n "
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
    print(f"  hidden: {cnt['ID']} identifiers, {cnt['STR']} strings, "
          f"{cnt['MLS']} multiline-string lines, {cnt['CMT']} comments")
    print(f"  kept visible (structure): keywords, @builtins, types, std API, "
          f"numbers, char literals, @import paths")
    print(f"  round-trip lossless: {n_lossless}/{len(files)} files")
    print(f"  leak scan over redacted output: "
          f"{'CLEAN — no redacted identifier or string appears' if not leaks else 'LEAKS: ' + ', '.join(leaks[:5])}")
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
    target = getattr(args, "target", "source")
    restore = rehydrate_rust_text if target == "rust" else rehydrate_text
    files = ([args.input] if os.path.isfile(args.input)
             else [os.path.join(r, fn)
                   for r, _, fs in os.walk(args.input) for fn in sorted(fs)])
    n = 0
    for src in files:
        text, _ = _read(src)
        # within a mixed results folder, .zig sources restore generically even
        # under --target rust; emitted .rs/.md get the emitter-aware inverse
        this_restore = (rehydrate_text
                        if src.lower().endswith(".zig") else restore)
        restored = this_restore(text, rev)
        with open(_out_path(args.input, src, args.out), "w",
                  encoding="utf-8") as f:
            f.write(restored)
        n += 1
    mode_note = (" (Rust-aware: emitter name transforms mirrored)"
                 if target == "rust" else "")
    print(f"rehydrated {n} file(s) -> {args.out}{mode_note}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL,
        description="Agentyx Zig redactor — redact locally, upload only "
                    "tokens, keep the keys.")
    ap.add_argument("--version", action="version", version=f"{TOOL} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("redact", help="redact a file or tree; write keys file")
    p.add_argument("--in", dest="input", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keys", required=True)
    p.add_argument("--ext", default=",".join(e[1:] for e in DEFAULT_EXTS),
                   help="comma-separated extensions (default: zig)")
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
    p.add_argument("--target", choices=("source", "rust"), default="source",
                   help="'rust' for results returned by the Agentyx Zig→Rust "
                        "service (.rs/.md): restores tokens AND mirrors the "
                        "emitter's name transforms (snake_case, r# escapes). "
                        "'source' (default) for plain token restore.")
    p.set_defaults(fn=cmd_rehydrate)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
