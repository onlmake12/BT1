# Q975: Low core restart reorg persistence in OnionServiceConfig

## Question
Can an unprivileged attacker shape serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through a local operator invoking a default-enabled node path that depends on this module, then force normal restart, reorg, retry, or replay handling so `OnionServiceConfig` in `util/onion/src/lib.rs` persists inconsistent state and trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/onion/src/lib.rs::OnionServiceConfig`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
