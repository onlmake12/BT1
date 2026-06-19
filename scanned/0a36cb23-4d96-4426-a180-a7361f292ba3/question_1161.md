# Q1161: High crypto resource amplification in From

## Question
Can an unprivileged attacker repeatedly send small network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings to make `From` in `util/crypto/src/secp/error.rs` amplify CPU, memory, storage, or bandwidth and make verification accept a malformed signature/proof/hash that should be rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/error.rs::From`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
