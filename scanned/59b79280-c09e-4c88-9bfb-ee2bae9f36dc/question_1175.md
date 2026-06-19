# Q1175: Critical crypto cache invalidation failure in random_privkey

## Question
Can an unprivileged attacker use a transaction sender supplying crafted signatures, hashes, script args, and witness data to alternate valid and invalid Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions so `random_privkey` in `util/crypto/src/secp/generator.rs` leaves a cache, index, or status flag stale and panic or overrun a cryptographic parser before a malformed object is rejected, violating hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/generator.rs::random_privkey`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: hash, Merkle, MMR, and fixed-byte conversions must be canonical across consensus and RPC paths
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
