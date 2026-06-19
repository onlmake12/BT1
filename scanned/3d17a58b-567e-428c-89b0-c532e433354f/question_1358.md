# Q1358: Critical crypto batch interaction bug in lib

## Question
Can an unprivileged attacker batch script args, witness lock fields, pubkey hash matching, and conversion boundaries through a script author relying on secp/multisig/hash utilities through system script behavior so `lib` in `util/multisig/src/lib.rs` handles the first item safely but applies incorrect assumptions to later items and trigger inconsistent serialization or byte-order interpretation between consensus and API paths, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/multisig/src/lib.rs::lib`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: trigger inconsistent serialization or byte-order interpretation between consensus and API paths
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
