# Q1176: High crypto state transition mismatch in secp

## Question
Can an unprivileged attacker enter through a transaction sender supplying crafted signatures, hashes, script args, and witness data and sequence Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions so `secp` in `util/crypto/src/secp/mod.rs` observes pre-state and post-state from different views, letting the flow make verification accept a malformed signature/proof/hash that should be rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/mod.rs::secp`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
