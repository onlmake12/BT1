# Q1000: High core cache invalidation failure in saturating_sub_u256

## Question
Can an unprivileged attacker use a block or transaction relayer triggering this helper during validation, sync, or storage updates to alternate valid and invalid serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values so `saturating_sub_u256` in `util/rational/src/lib.rs` leaves a cache, index, or status flag stale and make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rational/src/lib.rs::saturating_sub_u256`
- Entrypoint: a block or transaction relayer triggering this helper during validation, sync, or storage updates
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
