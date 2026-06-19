# Q1184: High crypto parser precheck gap in secp

## Question
Can an unprivileged attacker submit malformed-but-reachable public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings through a script author relying on secp/multisig/hash utilities through system script behavior so `secp` in `util/crypto/src/secp/mod.rs` performs expensive or unsafe work before validation and make verification accept a malformed signature/proof/hash that should be rejected, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/crypto/src/secp/mod.rs::secp`
- Entrypoint: a script author relying on secp/multisig/hash utilities through system script behavior
- Attacker controls: public keys, signatures, recovery IDs, multisig thresholds, message preimages, and hash encodings
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
