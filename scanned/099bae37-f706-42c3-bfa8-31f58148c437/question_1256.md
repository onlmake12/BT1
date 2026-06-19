# Q1256: Critical crypto cache invalidation failure in Eq

## Question
Can an unprivileged attacker use a block relayer supplying Merkle/MMR/proof-related data at boundary lengths to alternate valid and invalid public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings so `Eq` in `util/fixed-hash/core/src/std_cmp.rs` leaves a cache, index, or status flag stale and panic or overrun a cryptographic parser before a malformed object is rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fixed-hash/core/src/std_cmp.rs::Eq`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
