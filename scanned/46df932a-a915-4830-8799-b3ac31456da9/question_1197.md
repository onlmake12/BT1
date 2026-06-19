# Q1197: High crypto boundary divergence in Deref

## Question
Can an unprivileged attacker enter through a script author relying on secp/multisig/hash utilities through system script behavior and use script args, witness lock fields, pubkey hash matching, and conversion boundaries to drive `Deref` in `util/crypto/src/secp/pubkey.rs` across a boundary where make duplicate or empty proof elements produce a valid root for the wrong data, violating the invariant that hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/pubkey.rs::Deref`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
