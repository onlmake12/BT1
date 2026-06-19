# Q287: High cli resource amplification in track_current_process_simple

## Question
Can an unprivileged attacker repeatedly send small local database contents, malformed config files, and supported operator commands through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data to make `track_current_process_simple` in `util/memory-tracker/src/lib.rs` amplify CPU, memory, storage, or bandwidth and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/memory-tracker/src/lib.rs::track_current_process_simple`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
