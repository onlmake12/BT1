# Q386: Critical consensus parser precheck gap in find_unverified_blocks

## Question
Can an unprivileged attacker submit malformed-but-reachable genesis/spec fields on a private chain and canonical block metadata during replay through an RPC block submitter feeding locally generated consensus objects so `find_unverified_blocks` in `chain/src/init_load_unverified.rs` performs expensive or unsafe work before validation and trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating malformed consensus objects must return structured errors without node panic or unbounded work, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `chain/src/init_load_unverified.rs::find_unverified_blocks`
- Entrypoint: an RPC block submitter feeding locally generated consensus objects
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: malformed consensus objects must return structured errors without node panic or unbounded work
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
