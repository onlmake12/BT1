# Q1369: Critical crypto batch interaction bug in verify_m_of_n

## Question
Can an unprivileged attacker batch script args, witness lock fields, pubkey hash matching, and conversion boundaries through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `verify_m_of_n` in `util/multisig/src/secp256k1.rs` handles the first item safely but applies incorrect assumptions to later items and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/multisig/src/secp256k1.rs::verify_m_of_n`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
