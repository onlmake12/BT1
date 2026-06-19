# Q1212: Critical crypto boundary divergence in is_valid

## Question
Can an unprivileged attacker enter through a script author relying on secp/multisig/hash utilities through system script behavior and use public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings to drive `is_valid` in `util/crypto/src/secp/signature.rs` across a boundary where trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating the invariant that hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/signature.rs::is_valid`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
