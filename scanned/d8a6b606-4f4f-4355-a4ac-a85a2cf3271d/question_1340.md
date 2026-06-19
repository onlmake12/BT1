# Q1340: Critical crypto parser precheck gap in empty_blake2b

## Question
Can an unprivileged attacker submit malformed-but-reachable public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `empty_blake2b` in `util/hash/src/lib.rs` performs expensive or unsafe work before validation and panic or overrun a cryptographic parser before a malformed object is rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/hash/src/lib.rs::empty_blake2b`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
