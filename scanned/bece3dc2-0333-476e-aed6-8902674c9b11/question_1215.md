# Q1215: Critical crypto limit off by one in recover

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for script args, witness lock fields, pubkey hash matching, and conversion boundaries through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `recover` in `util/crypto/src/secp/signature.rs` make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/crypto/src/secp/signature.rs::recover`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
