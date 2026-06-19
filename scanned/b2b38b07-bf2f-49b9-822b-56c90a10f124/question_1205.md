# Q1205: High crypto differential path split in from_slice

## Question
Can an unprivileged attacker reach `from_slice` in `util/crypto/src/secp/pubkey.rs` through two production paths from a transaction sender supplying crafted signatures, hashes, script args, and witness data and make one path accept while the other rejects because of public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/pubkey.rs::from_slice`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
