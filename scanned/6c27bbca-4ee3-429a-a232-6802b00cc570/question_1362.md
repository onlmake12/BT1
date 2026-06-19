# Q1362: Critical crypto boundary divergence in lib

## Question
Can an unprivileged attacker enter through a transaction sender supplying crafted signatures, hashes, script args, and witness data and use script args, witness lock fields, pubkey hash matching, and conversion boundaries to drive `lib` in `util/multisig/src/lib.rs` across a boundary where make duplicate or empty proof elements produce a valid root for the wrong data, violating the invariant that malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/multisig/src/lib.rs::lib`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: script args, witness lock fields, pubkey hash matching, and conversion boundaries
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
