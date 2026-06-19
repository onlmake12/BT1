# Q1739: Critical network batch interaction bug in async_future_task

## Question
Can an unprivileged attacker batch peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing through a transaction/block relayer sending repeated malformed-but-cheap payloads so `async_future_task` in `network/src/protocols/mod.rs` handles the first item safely but applies incorrect assumptions to later items and trigger a parser or protocol-state panic with a single malformed peer-controlled payload, violating peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/mod.rs::async_future_task`
- Entrypoint: a transaction/block relayer sending repeated malformed-but-cheap payloads
- Attacker controls: peer IDs, multiaddrs, discovery counts, timestamps, ban/score-relevant fields, and connection timing
- Exploit idea: trigger a parser or protocol-state panic with a single malformed peer-controlled payload
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
