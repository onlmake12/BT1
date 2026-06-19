# Q1409: High crypto limit off by one in Verifier

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a script author relying on secp/multisig/hash utilities through system script behavior so `Verifier` in `util/network-alert/src/verifier.rs` make duplicate or empty proof elements produce a valid root for the wrong data, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/network-alert/src/verifier.rs::Verifier`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
