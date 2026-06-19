# Q1292: Critical crypto parser precheck gap in Display

## Question
Can an unprivileged attacker submit malformed-but-reachable Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions through a transaction sender supplying crafted signatures, hashes, script args, and witness data so `Display` in `util/fixed-hash/core/src/std_fmt.rs` performs expensive or unsafe work before validation and make duplicate or empty proof elements produce a valid root for the wrong data, violating malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/fixed-hash/core/src/std_fmt.rs::Display`
- Entrypoint: a transaction sender supplying crafted signatures, hashes, script args, and witness data
- Attacker controls: Merkle leaves, proof ordering, duplicate hashes, empty roots, and MMR positions
- Exploit idea: make duplicate or empty proof elements produce a valid root for the wrong data
- Invariant to test: malformed signatures, proofs, alerts, or hashes must never crash a node or bypass authorization
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a property or unit test over signatures, hashes, proofs, and encodings; assert malformed variants reject without panic.
