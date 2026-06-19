# Q1209: High crypto differential path split in from_rsv

## Question
Can an unprivileged attacker reach `from_rsv` in `util/crypto/src/secp/signature.rs` through two production paths from a transaction sender supplying crafted signatures, hashes, script args, and witness data and make one path accept while the other rejects because of Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions, violating system-script-visible crypto behavior must match consensus expectations, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/signature.rs::from_rsv`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
