# Q1174: Critical crypto cache invalidation failure in random_privkey

## Question
Can an unprivileged attacker use a peer relaying network alerts or consensus objects with adversarial cryptographic encodings to alternate valid and invalid script args, witness lock fields, pubkey hash matching, and conversion boundaries so `random_privkey` in `util/crypto/src/secp/generator.rs` leaves a cache, index, or status flag stale and make verification accept a malformed signature/proof/hash that should be rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/generator.rs::random_privkey`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
