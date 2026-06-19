# Q1214: Critical crypto restart reorg persistence in is_valid

## Question
Can an unprivileged attacker shape script args, witness lock fields, pubkey hash matching, and conversion boundaries through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings, then force normal restart, reorg, retry, or replay handling so `is_valid` in `util/crypto/src/secp/signature.rs` persists inconsistent state and panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/signature.rs::is_valid`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
