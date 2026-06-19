# Q1194: High crypto differential path split in pubkey

## Question
Can an unprivileged attacker reach `pubkey` in `util/crypto/src/secp/privkey.rs` through two production paths from a block relayer supplying Merkle/MMR/proof-related data at boundary lengths and make one path accept while the other rejects because of public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings, violating system-script-visible crypto behavior must match consensus expectations, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/privkey.rs::pubkey`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
