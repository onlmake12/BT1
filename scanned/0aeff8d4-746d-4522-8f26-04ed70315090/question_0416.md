# Q416: Critical consensus limit off by one in new

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for fork order, orphan arrival timing, hardfork activation boundary, and reorg depth through a remote peer relaying a crafted block/header sequence so `new` in `chain/src/preload_unverified_blocks_channel.rs` trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating malformed consensus objects must return structured errors without node panic or unbounded work, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `chain/src/preload_unverified_blocks_channel.rs::new`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: fork order, orphan arrival timing, hardfork activation boundary, and reorg depth
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: malformed consensus objects must return structured errors without node panic or unbounded work
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
