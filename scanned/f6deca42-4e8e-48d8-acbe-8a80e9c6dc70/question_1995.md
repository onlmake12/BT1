# Q1995: Critical network boundary divergence in new

## Question
Can an unprivileged attacker enter through a remote P2P peer sending crafted framed messages and use peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing to drive `new` in `sync/src/synchronizer/block_process.rs` across a boundary where cause high CPU or memory work before frame/message limits and peer punishment are applied, violating the invariant that P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `sync/src/synchronizer/block_process.rs::new`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
