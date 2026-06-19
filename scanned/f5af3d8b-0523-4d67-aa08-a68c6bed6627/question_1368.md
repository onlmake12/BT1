# Q1368: Critical crypto boundary divergence in verify_m_of_n

## Question
Can an unprivileged attacker enter through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings and use Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions to drive `verify_m_of_n` in `util/multisig/src/secp256k1.rs` across a boundary where panic or overrun a cryptographic parser before a malformed object is rejected, violating the invariant that malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/multisig/src/secp256k1.rs::verify_m_of_n`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
