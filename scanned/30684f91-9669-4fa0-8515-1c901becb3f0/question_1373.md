# Q1373: Critical crypto resource amplification in verify_m_of_n

## Question
Can an unprivileged attacker repeatedly send small script args, witness lock fields, pubkey hash matching, and conversion boundaries through a script author relying on secp/multisig/hash utilities through system script behavior to make `verify_m_of_n` in `util/multisig/src/secp256k1.rs` amplify CPU, memory, storage, or bandwidth and panic or overrun a cryptographic parser before a malformed object is rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/multisig/src/secp256k1.rs::verify_m_of_n`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
