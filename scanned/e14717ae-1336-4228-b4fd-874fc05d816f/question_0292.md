# Q292: Low cli differential path split in get_current_process_memory

## Question
Can an unprivileged attacker reach `get_current_process_memory` in `util/memory-tracker/src/process.rs` through two production paths from a local process starting, stopping, importing, exporting, replaying, or migrating CKB data and make one path accept while the other rejects because of CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options, violating operator-facing services must not crash or degrade the node through valid local inputs, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/memory-tracker/src/process.rs::get_current_process_memory`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
