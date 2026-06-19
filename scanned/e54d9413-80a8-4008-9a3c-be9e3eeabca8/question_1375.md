# Q1375: High crypto restart reorg persistence in verify_m_of_n

## Question
Can an unprivileged attacker shape Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a transaction sender supplying crafted signatures, hashes, script args, and witness data, then force normal restart, reorg, retry, or replay handling so `verify_m_of_n` in `util/multisig/src/secp256k1.rs` persists inconsistent state and panic or overrun a cryptographic parser before a malformed object is rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/multisig/src/secp256k1.rs::verify_m_of_n`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
