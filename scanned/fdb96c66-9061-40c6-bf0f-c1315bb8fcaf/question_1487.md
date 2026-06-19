# Q1487: Critical network replay reorder race in From

## Question
Can an unprivileged attacker replay, reorder, or delay header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a discovery peer advertising adversarial addresses and node records so `From` in `network/src/network_group.rs` takes a stale branch and cause high CPU or memory work before frame/message limits and peer punishment are applied, breaking the invariant that peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/network_group.rs::From`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
