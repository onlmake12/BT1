# Q203: High cli restart reorg persistence in ExitCode

## Question
Can an unprivileged attacker shape runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data, then force normal restart, reorg, retry, or replay handling so `ExitCode` in `util/app-config/src/exit_code.rs` persists inconsistent state and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/exit_code.rs::ExitCode`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
