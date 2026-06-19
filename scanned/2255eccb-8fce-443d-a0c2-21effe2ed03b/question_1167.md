# Q1167: Critical crypto batch interaction bug in Generator

## Question
Can an unprivileged attacker batch script args, witness lock fields, pubkey hash matching, and conversion boundaries through a block relayer supplying Merkle/MMR/proof-related data at boundary lengths so `Generator` in `util/crypto/src/secp/generator.rs` handles the first item safely but applies incorrect assumptions to later items and make verification accept a malformed signature/proof/hash that should be rejected, violating system-script-visible crypto behavior must match consensus expectations, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/crypto/src/secp/generator.rs::Generator`
- Entrypoint: a block relayer supplying Merkle/MMR/proof-related data at boundary lengths
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make verification accept a malformed signature/proof/hash that should be rejected
- Invariant to test: system-script-visible crypto behavior must match consensus expectations
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
