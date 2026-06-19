# Q1337: Critical crypto cache invalidation failure in blake2b_256

## Question
Can an unprivileged attacker use a transaction sender supplying crafted signatures, hashes, script args, and witness data to alternate valid and invalid script args, witness lock fields, pubkey hash matching, and conversion boundaries so `blake2b_256` in `util/hash/src/lib.rs` leaves a cache, index, or status flag stale and make verification accept a malformed signature/proof/hash that should be rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/hash/src/lib.rs::blake2b_256`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
