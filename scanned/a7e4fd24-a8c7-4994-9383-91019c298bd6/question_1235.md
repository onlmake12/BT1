# Q1235: Critical crypto canonical encoding ambiguity in from_slice

## Question
Can an unprivileged attacker craft alternate encodings for script args, witness lock fields, pubkey hash matching, and conversion boundaries through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings so `from_slice` in `util/fixed-hash/core/src/impls.rs` accepts two representations for one security object and make verification accept a malformed signature/proof/hash that should be rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/impls.rs::from_slice`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
