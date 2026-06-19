# Q1314: Critical crypto cache invalidation failure in from_trimmed_str

## Question
Can an unprivileged attacker use a script author relying on secp/multisig/hash utilities through system script behavior to alternate valid and invalid Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions so `from_trimmed_str` in `util/fixed-hash/core/src/std_str.rs` leaves a cache, index, or status flag stale and make verification accept a malformed signature/proof/hash that should be rejected, violating cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fixed-hash/core/src/std_str.rs::from_trimmed_str`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: cryptographic checks must bind exactly to the intended message, domain, key, threshold, and serialized bytes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
