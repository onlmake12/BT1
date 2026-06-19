# Q2: High cli canonical encoding ambiguity in export

## Question
Can an unprivileged attacker craft alternate encodings for local database contents, malformed config files, and supported operator commands through an operator using default-enabled configuration generated or parsed by the node so `export` in `ckb-bin/src/cli.rs` accepts two representations for one security object and crash the command or node through supported local input before validation or recovery runs, violating operator-facing services must not crash or degrade the node through valid local inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/cli.rs::export`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
