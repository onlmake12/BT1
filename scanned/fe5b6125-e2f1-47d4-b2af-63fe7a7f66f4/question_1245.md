# Q1245: Critical crypto parser precheck gap in H520

## Question
Can an unprivileged attacker submit malformed-but-reachable network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `H520` in `util/fixed-hash/core/src/lib.rs` performs expensive or unsafe work before validation and panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fixed-hash/core/src/lib.rs::H520`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
