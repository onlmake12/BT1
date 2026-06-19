# Q1218: Critical crypto parser precheck gap in FromSliceError

## Question
Can an unprivileged attacker submit malformed-but-reachable public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a script author relying on secp/multisig/hash utilities through system script behavior so `FromSliceError` in `util/fixed-hash/core/src/error.rs` performs expensive or unsafe work before validation and make duplicate or empty proof elements produce a valid root for the wrong data, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/error.rs::FromSliceError`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
