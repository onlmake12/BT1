# Q117: Low cli restart reorg persistence in ExportTarget

## Question
Can an unprivileged attacker shape CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator using default-enabled configuration generated or parsed by the node, then force normal restart, reorg, retry, or replay handling so `ExportTarget` in `util/app-config/src/args.rs` persists inconsistent state and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/args.rs::ExportTarget`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
