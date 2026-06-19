# Q1953: Critical network boundary divergence in new

## Question
Can an unprivileged attacker enter through a discovery peer advertising adversarial addresses and node records and use message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths to drive `new` in `sync/src/relayer/get_block_proposal_process.rs` across a boundary where make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating the invariant that peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `sync/src/relayer/get_block_proposal_process.rs::new`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
