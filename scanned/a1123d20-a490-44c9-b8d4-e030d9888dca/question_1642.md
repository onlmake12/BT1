# Q1642: High network parser precheck gap in to_inner

## Question
Can an unprivileged attacker submit malformed-but-reachable header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a remote P2P peer sending crafted framed messages so `to_inner` in `network/src/protocols/discovery/state.rs` performs expensive or unsafe work before validation and make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `network/src/protocols/discovery/state.rs::to_inner`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: P2P inputs must be bounded, deterministic, and rejected before expensive work or state mutation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
