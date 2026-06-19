# Q1366: Critical crypto cache invalidation failure in verify_m_of_n

## Question
Can an unprivileged attacker use a peer relaying network alerts or consensus objects with adversarial cryptographic encodings to alternate valid and invalid Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions so `verify_m_of_n` in `util/multisig/src/secp256k1.rs` leaves a cache, index, or status flag stale and panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/multisig/src/secp256k1.rs::verify_m_of_n`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
