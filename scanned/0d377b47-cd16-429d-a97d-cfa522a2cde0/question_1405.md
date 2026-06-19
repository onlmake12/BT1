# Q1405: Critical crypto parser precheck gap in received_alerts

## Question
Can an unprivileged attacker submit malformed-but-reachable Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a peer relaying network alerts or consensus objects with adversarial cryptographic encodings so `received_alerts` in `util/network-alert/src/notifier.rs` performs expensive or unsafe work before validation and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/notifier.rs::received_alerts`
- Entrypoint: a peer relaying network alerts or consensus objects with adversarial cryptographic encodings
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
