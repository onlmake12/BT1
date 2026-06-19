# Q1361: Critical crypto cache invalidation failure in lib

## Question
Can an unprivileged attacker use a script author relying on secp/multisig/hash utilities through system script behavior to alternate valid and invalid script args, witness lock fields, pubkey hash matching, and conversion boundaries so `lib` in `util/multisig/src/lib.rs` leaves a cache, index, or status flag stale and make duplicate or empty proof elements produce a valid root for the wrong data, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/multisig/src/lib.rs::lib`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
