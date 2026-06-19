# Q1384: Critical crypto parser precheck gap in notifier

## Question
Can an unprivileged attacker submit malformed-but-reachable network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `notifier` in `util/network-alert/src/alert_relayer.rs` performs expensive or unsafe work before validation and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/network-alert/src/alert_relayer.rs::notifier`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
