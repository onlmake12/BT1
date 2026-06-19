# Q1308: Critical crypto batch interaction bug in $name

## Question
Can an unprivileged attacker batch public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a script author relying on secp/multisig/hash utilities through system script behavior so `$name` in `util/fixed-hash/core/src/std_str.rs` handles the first item safely but applies incorrect assumptions to later items and make duplicate or empty proof elements produce a valid root for the wrong data, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_str.rs::$name`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
