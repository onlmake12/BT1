# Q1293: High crypto differential path split in LowerHex

## Question
Can an unprivileged attacker reach `LowerHex` in `util/fixed-hash/core/src/std_fmt.rs` through two production paths from a peer relaying network alerts or consensus objects with adversarial cryptographic encodings and make one path accept while the other rejects because of Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_fmt.rs::LowerHex`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
