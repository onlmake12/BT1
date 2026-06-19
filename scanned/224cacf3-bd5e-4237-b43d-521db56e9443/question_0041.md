# Q41: High cli batch interaction bug in init

## Question
Can an unprivileged attacker batch CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `init` in `ckb-bin/src/subcommand/init.rs` handles the first item safely but applies incorrect assumptions to later items and crash the command or node through supported local input before validation or recovery runs, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/init.rs::init`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
