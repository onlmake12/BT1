# Q1309: High crypto canonical encoding ambiguity in $name

## Question
Can an unprivileged attacker craft alternate encodings for public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a script author relying on secp/multisig/hash utilities through system script behavior so `$name` in `util/fixed-hash/core/src/std_str.rs` accepts two representations for one security object and make verification accept a malformed signature/proof/hash that should be rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_str.rs::$name`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
