# Q261: High cli restart reorg persistence in new

## Question
Can an unprivileged attacker shape CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator using default-enabled configuration generated or parsed by the node, then force normal restart, reorg, retry, or replay handling so `new` in `util/launcher/src/lib.rs` persists inconsistent state and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/launcher/src/lib.rs::new`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
