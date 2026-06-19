# Q305: High cli resource amplification in Target

## Question
Can an unprivileged attacker repeatedly send small CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator-facing component processing log, metrics, memory, runtime, or launcher state to make `Target` in `util/metrics-config/src/lib.rs` amplify CPU, memory, storage, or bandwidth and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/metrics-config/src/lib.rs::Target`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
