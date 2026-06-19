# Q186: High cli cross module inconsistency in integration_test_enable

## Question
Can an unprivileged attacker use a local process starting, stopping, importing, exporting, replaying, or migrating CKB data to make `integration_test_enable` in `util/app-config/src/configs/rpc.rs` return a result that downstream modules interpret differently, where make generated defaults enable an unsafe resource or performance behavior in normal operation, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/rpc.rs::integration_test_enable`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
