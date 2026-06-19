# Q1165: Critical crypto cache invalidation failure in from

## Question
Can an unprivileged attacker use a transaction sender supplying crafted signatures, hashes, script args, and witness data to alternate valid and invalid Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions so `from` in `util/crypto/src/secp/error.rs` leaves a cache, index, or status flag stale and make verification accept a malformed signature/proof/hash that should be rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/crypto/src/secp/error.rs::from`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
