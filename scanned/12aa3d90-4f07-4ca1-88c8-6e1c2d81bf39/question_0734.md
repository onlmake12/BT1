# Q734: Low core parser precheck gap in is_dirty

## Question
Can an unprivileged attacker submit malformed-but-reachable serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `is_dirty` in `util/build-info/src/lib.rs` performs expensive or unsafe work before validation and make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/build-info/src/lib.rs::is_dirty`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
