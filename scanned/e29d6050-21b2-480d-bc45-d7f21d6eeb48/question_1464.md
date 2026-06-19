# Q1464: High network boundary divergence in from

## Question
Can an unprivileged attacker enter through a remote P2P peer sending crafted framed messages and use message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths to drive `from` in `network/src/errors.rs` across a boundary where desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, violating the invariant that peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `network/src/errors.rs::from`
- Entrypoint: a remote P2P peer sending crafted framed messages
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
