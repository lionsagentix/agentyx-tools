# Agentyx Tools — redact locally, keep the keys

Open-source client-side tools for [Agentyx](https://agentyx.dev) privacy-preserving
code modernization. You run these **on your own machine**: they replace every
name, string literal and comment in your source with neutral tokens (`ID_1`,
`STR_2`, `CMT_3` …) and write the token→original map to a **keys file that never
leaves your machine**. You upload only the redacted code; Agentyx converts,
tests and documents it without ever seeing a single proprietary name; you
restore the results locally with your keys.

**Why you can trust these tools**

- **One auditable file per language.** Each tool is a single Python script with
  no dependencies beyond the Python 3 standard library. Read it before you run it.
- **No network access, ever.** There is no upload code in this repository.
  Verify yourself: `grep -niE "socket|urllib|http|request" *.py` matches only
  the Apache license URL in each file's header comment.
- **Self-verifying.** `redact` rehydrates its own output in memory and confirms
  it is byte-identical to your original, then scans the redacted output to
  confirm no redacted name or literal survived. A separate `verify` command
  lets you re-check independently at any time.
- **You hold the keys.** The keys file is the only way to map tokens back to
  your names. Agentyx never receives it.

## Tools

| language | tool | redacts | keeps (structure the conversion needs) |
|---|---|---|---|
| COBOL | `agentyx_redact_cobol.py` | data/paragraph names, string & hex literals, value numbers, comment text | reserved words, PIC clauses, level numbers, OCCURS counts, layout |
| Zig | `agentyx_redact_zig.py` | identifiers, string literals, multiline strings, comment text | keywords, @builtins, types, std-library API, numbers, char literals, `@import` paths |

More languages are added on request — [contact Agentyx](https://agentyx.dev).

## Quickstart

Requires Python 3.8+. Download the script for your language (or clone this
repo), then:

```bash
# 1. Redact — locally, offline. Writes redacted code + your private keys file.
python3 agentyx_redact_cobol.py redact \
    --in ./src --out ./redacted --keys ./agentyx.keys.json

# 2. (Optional but recommended) Independently verify before uploading:
#    every redacted file rehydrates byte-for-byte, and leaks nothing.
python3 agentyx_redact_cobol.py verify \
    --original ./src --redacted ./redacted --keys ./agentyx.keys.json

# 3. Upload ONLY ./redacted to Agentyx. Never upload agentyx.keys.json.

# 4. When results come back, restore your real names locally:
python3 agentyx_redact_cobol.py rehydrate \
    --in ./results --out ./final --keys ./agentyx.keys.json
```

`agentyx_redact_zig.py` has the identical interface.

The whole tree shares one keys map, so the same name becomes the same token in
every file and copybook — conversion needs that consistency.

### Restoring conversion results (Zig → Rust)

Results returned by the Agentyx Zig → Rust service (converted `.rs`, generated
tests, API docs) are still in tokens. Restore the whole results folder with one
command — use `--target rust` so the tool also mirrors the converter's
deterministic naming rules (camelCase → snake_case, `r#` keyword escapes)
instead of pasting raw Zig names into Rust:

```bash
python3 agentyx_redact_zig.py rehydrate \
    --in ./results --out ./final --keys ./math.zig.agentyx.keys.json --target rust
```

This is a faithful inverse: restoring the redacted results equals converting
your real code directly, byte for byte (matched against Agentyx Zig→Rust
emitter v1.10.x; the test suite covers the transforms).

### Example — what crosses the wire

Your code:
```cobol
01  WS-FEE-RATE   PIC 9(2)V99 VALUE 2.50.
IF WS-CUSTOMER-NAME = 'ACME HOLDINGS'
   DISPLAY 'VIP CLIENT: ' WS-CUSTOMER-NAME
```
What Agentyx sees:
```cobol
01  DN_1   PIC 9(2)V99 VALUE NUM_1.
IF DN_2 = STR_1
   DISPLAY STR_2 DN_2
```

## The keys file

`agentyx.keys.json` contains the full token→original map plus metadata
(`format: agentyx-keys/1`). Treat it like source code: keep it under your normal
source-code controls, and **never upload or share it**.

Redaction is **deterministic**: the same tool version run on the byte-identical
source tree always produces the same tokens and the same keys file (the test
suite enforces this). So a lost keys file is recoverable — re-run `redact` on
the unchanged original source and the regenerated keys restore any previously
returned results. If the source has changed (or the tool version differs),
token numbering shifts and old results cannot be restored — which is why
keeping the keys file under version control next to the source is still the
right habit.

## Honest limits — read before relying on this

1. **Structure stays visible.** Control flow, record layouts, type shapes and
   the call graph are exactly what conversion consumes, so they are exactly
   what is not hidden. If your secret is the *algorithm's shape* rather than
   its names and values, redaction alone is not enough — talk to us about
   additional measures.
2. **Per-language keep policies.** COBOL keeps level numbers and OCCURS counts;
   Zig keeps numeric/char literals (they are routinely structural — array
   sizes, comptime arithmetic) and `@import` paths (module wiring; file names
   are disclosed). Every `redact` run prints exactly what was hidden and what
   was kept.
3. **Tokenization is consistent.** The same name always maps to the same token —
   that is required for conversion, and it means equality patterns between
   redacted names are visible (how often a token occurs, which tokens appear
   together).
4. Files must be UTF-8 (or ASCII) text; the tool warns if it cannot decode a
   file losslessly.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers byte-lossless round-trips, leak scans, structure preservation,
cross-file token consistency, the collision guard (refusing already-redacted
input), and the full CLI flow for both languages.

## License

Apache-2.0 — see [LICENSE](LICENSE).
