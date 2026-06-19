# Q1183: High crypto resource amplification in secp

## Question
Can an unprivileged attacker repeatedly send small script args, witness lock fields, pubkey hash matching, and conversion boundaries through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings to make `secp` in `util/crypto/src/secp/mod.rs` amplify CPU, memory, storage, or bandwidth and make duplicate or empty proof elements produce a valid root for the wrong data, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/mod.rs::secp`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
