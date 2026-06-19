# Q1853: Critical network restart reorg persistence in new

## Question
Can an unprivileged attacker shape peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing through a discovery peer advertising adversarial addresses and node records, then force normal restart, reorg, retry, or replay handling so `new` in `sync/src/filter/get_block_filter_check_points_process.rs` persists inconsistent state and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `sync/src/filter/get_block_filter_check_points_process.rs::new`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
