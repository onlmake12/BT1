# Q1259: High crypto parser precheck gap in Ord

## Question
Can an unprivileged attacker submit malformed-but-reachable network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `Ord` in `util/fixed-hash/core/src/std_cmp.rs` performs expensive or unsafe work before validation and panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/fixed-hash/core/src/std_cmp.rs::Ord`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
