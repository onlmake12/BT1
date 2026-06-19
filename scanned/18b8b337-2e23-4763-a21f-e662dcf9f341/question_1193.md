# Q1193: Critical crypto cache invalidation failure in from_str

## Question
Can an unprivileged attacker use a block relayer supplying Merkle/MMR/proof-related data at boundary lengths to alternate valid and invalid public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings so `from_str` in `util/crypto/src/secp/privkey.rs` leaves a cache, index, or status flag stale and panic or overrun a cryptographic parser before a malformed object is rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/privkey.rs::from_str`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
