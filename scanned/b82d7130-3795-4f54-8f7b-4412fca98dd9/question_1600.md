# Q1600: Critical network batch interaction bug in disconnected

## Question
Can an unprivileged attacker batch header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses through a discovery peer advertising adversarial addresses and node records so `disconnected` in `network/src/protocols/disconnect_message.rs` handles the first item safely but applies incorrect assumptions to later items and desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, violating peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/protocols/disconnect_message.rs::disconnected`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
