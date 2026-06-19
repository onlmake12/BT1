# Q212: High cli restart reorg persistence in new

## Question
Can an unprivileged attacker shape local database contents, malformed config files, and supported operator commands through an operator using default-enabled configuration generated or parsed by the node, then force normal restart, reorg, retry, or replay handling so `new` in `util/app-config/src/legacy/mod.rs` persists inconsistent state and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/legacy/mod.rs::new`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
