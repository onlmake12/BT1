# Q1233: Critical crypto canonical encoding ambiguity in from_slice

## Question
Can an unprivileged attacker craft alternate encodings for public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `from_slice` in `util/fixed-hash/core/src/impls.rs` accepts two representations for one security object and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/impls.rs::from_slice`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
