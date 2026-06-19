# Q1263: Critical crypto canonical encoding ambiguity in eq

## Question
Can an unprivileged attacker craft alternate encodings for public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `eq` in `util/fixed-hash/core/src/std_cmp.rs` accepts two representations for one security object and panic or overrun a cryptographic parser before a malformed object is rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_cmp.rs::eq`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
