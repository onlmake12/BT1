# Q1854: High network parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing through a discovery peer advertising adversarial addresses and node records so `new` in `sync/src/filter/get_block_filter_check_points_process.rs` performs expensive or unsafe work before validation and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `sync/src/filter/get_block_filter_check_points_process.rs::new`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
