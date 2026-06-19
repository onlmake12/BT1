# Q161: High cli parser precheck gap in max_outbound_peers

## Question
Can an unprivileged attacker submit malformed-but-reachable CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator using default-enabled configuration generated or parsed by the node so `max_outbound_peers` in `util/app-config/src/configs/network.rs` performs expensive or unsafe work before validation and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/network.rs::max_outbound_peers`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
