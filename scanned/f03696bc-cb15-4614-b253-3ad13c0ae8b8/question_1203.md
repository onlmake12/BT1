# Q1203: High crypto canonical encoding ambiguity in fmt

## Question
Can an unprivileged attacker craft alternate encodings for network-alert payload bytes, serialization format, byte order, and fixed-hash lengths through a script author relying on secp/multisig/hash utilities through system script behavior so `fmt` in `util/crypto/src/secp/pubkey.rs` accepts two representations for one security object and panic or overrun a cryptographic parser before a malformed object is rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/pubkey.rs::fmt`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: network-alert payload bytes, serialization format, byte order, and fixed-hash lengths
- Exploit idea: panic or overrun a cryptographic parser before a malformed object is rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
