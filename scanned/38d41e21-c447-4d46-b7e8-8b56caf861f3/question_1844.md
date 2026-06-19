# Q1844: High network parser precheck gap in handle_watch_new_block

## Question
Can an unprivileged attacker submit malformed-but-reachable message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a discovery peer advertising adversarial addresses and node records so `handle_watch_new_block` in `notify/src/lib.rs` performs expensive or unsafe work before validation and desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `notify/src/lib.rs::handle_watch_new_block`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
