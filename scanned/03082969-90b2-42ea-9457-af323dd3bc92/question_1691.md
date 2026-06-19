# Q1691: High network cache invalidation failure in init_request

## Question
Can an unprivileged attacker use a transaction/block relayer sending repeated malformed-but-cheap payloads to alternate valid and invalid header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses so `init_request` in `network/src/protocols/hole_punching/component/mod.rs` leaves a cache, index, or status flag stale and cause high CPU or memory work before frame/message limits and peer punishment are applied, violating peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `network/src/protocols/hole_punching/component/mod.rs::init_request`
- Entrypoint: a transaction/block relayer sending repeated malformed-but-cheap payloads
- Attacker controls: header batches, block locators, compact-block short IDs, transaction indexes, and missing-data responses
- Exploit idea: cause high CPU or memory work before frame/message limits and peer punishment are applied
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
