# Q1372: Critical crypto boundary divergence in verify_m_of_n

## Question
Can an unprivileged attacker enter through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings and use Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions to drive `verify_m_of_n` in `util/multisig/src/secp256k1.rs` across a boundary where make duplicate or empty proof elements produce a valid root for the wrong data, violating the invariant that system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/multisig/src/secp256k1.rs::verify_m_of_n`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
