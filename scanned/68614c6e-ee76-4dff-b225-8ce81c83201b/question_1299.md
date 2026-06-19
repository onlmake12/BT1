# Q1299: Critical crypto resource amplification in Hash

## Question
Can an unprivileged attacker repeatedly send small Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a script author relying on secp/multisig/hash utilities through system script behavior to make `Hash` in `util/fixed-hash/core/src/std_hash.rs` amplify CPU, memory, storage, or bandwidth and make verification accept a malformed signature/proof/hash that should be rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_hash.rs::Hash`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
