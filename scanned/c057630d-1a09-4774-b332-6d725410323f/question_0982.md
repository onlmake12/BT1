# Q982: Low core limit off by one in load_or_create_tor_secret_key

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs through a local operator invoking a default-enabled node path that depends on this module so `load_or_create_tor_secret_key` in `util/onion/src/onion_service.rs` trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/onion/src/onion_service.rs::load_or_create_tor_secret_key`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
