# Q1188: High crypto cache invalidation failure in drop

## Question
Can an unprivileged attacker use a transaction sender supplying crafted signatures, hashes, script args, and witness data to alternate valid and invalid Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions so `drop` in `util/crypto/src/secp/privkey.rs` leaves a cache, index, or status flag stale and make verification accept a malformed signature/proof/hash that should be rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/privkey.rs::drop`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
