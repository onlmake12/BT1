# Q1284: Critical crypto differential path split in default

## Question
Can an unprivileged attacker reach `default` in `util/fixed-hash/core/src/std_default.rs` through two production paths from a transaction sender supplying crafted signatures, hashes, script args, and witness data and make one path accept while the other rejects because of script args, witness lock fields, pubkey hash matching, and conversion boundaries, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/std_default.rs::default`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
