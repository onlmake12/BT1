# Q1225: Critical crypto restart reorg persistence in FromStrError

## Question
Can an unprivileged attacker shape script args, witness lock fields, pubkey hash matching, and conversion boundaries through a transaction sender supplying crafted signatures, hashes, script args, and witness data, then force normal restart, reorg, retry, or replay handling so `FromStrError` in `util/fixed-hash/core/src/error.rs` persists inconsistent state and panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/error.rs::FromStrError`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
