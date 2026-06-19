# Q248: High cli limit off by one in new

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for local database contents, malformed config files, and supported operator commands through a local command-line user invoking supported CKB subcommands with crafted arguments so `new` in `util/instrument/src/import.rs` crash the command or node through supported local input before validation or recovery runs, violating operator-facing services must not crash or degrade the node through valid local inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/instrument/src/import.rs::new`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
