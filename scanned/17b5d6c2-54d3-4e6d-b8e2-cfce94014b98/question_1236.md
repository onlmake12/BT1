# Q1236: Critical crypto boundary divergence in H160

## Question
Can an unprivileged attacker enter through a script author relying on secp/multisig/hash utilities through system script behavior and use script args, witness lock fields, pubkey hash matching, and conversion boundaries to drive `H160` in `util/fixed-hash/core/src/lib.rs` across a boundary where panic or overrun a cryptographic parser before a malformed object is rejected, violating the invariant that malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/lib.rs::H160`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
