# Q1198: Critical crypto state transition mismatch in From

## Question
Can an unprivileged attacker enter through a transaction sender supplying crafted signatures, hashes, script args, and witness data and sequence script args, witness lock fields, pubkey hash matching, and conversion boundaries so `From` in `util/crypto/src/secp/pubkey.rs` observes pre-state and post-state from different views, letting the flow make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/crypto/src/secp/pubkey.rs::From`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
