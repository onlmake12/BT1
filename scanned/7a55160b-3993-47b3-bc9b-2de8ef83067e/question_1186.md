# Q1186: Critical crypto resource amplification in Drop

## Question
Can an unprivileged attacker repeatedly send small Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a script author relying on secp/multisig/hash utilities through system script behavior to make `Drop` in `util/crypto/src/secp/privkey.rs` amplify CPU, memory, storage, or bandwidth and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/crypto/src/secp/privkey.rs::Drop`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
