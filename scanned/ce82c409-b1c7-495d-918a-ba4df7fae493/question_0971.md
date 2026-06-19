# Q971: Low core cache invalidation failure in OnionServiceConfig

## Question
Can an unprivileged attacker use a local operator invoking a default-enabled node path that depends on this module to alternate valid and invalid message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs so `OnionServiceConfig` in `util/onion/src/lib.rs` leaves a cache, index, or status flag stale and make canonical serialization or conversion accept an ambiguous representation, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/onion/src/lib.rs::OnionServiceConfig`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
