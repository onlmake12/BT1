# Q310: Low cli resource amplification in start_prometheus_service

## Question
Can an unprivileged attacker repeatedly send small CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data to make `start_prometheus_service` in `util/metrics-service/src/lib.rs` amplify CPU, memory, storage, or bandwidth and cause important performance degradation in a default-enabled operator path with small local input, violating operator-facing services must not crash or degrade the node through valid local inputs, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/metrics-service/src/lib.rs::start_prometheus_service`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
