# Q120: High cli limit off by one in cli

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `cli` in `util/app-config/src/cli.rs` cause important performance degradation in a default-enabled operator path with small local input, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/cli.rs::cli`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
