# Q1918: Critical network batch interaction bug in BlockUnclesVerifier

## Question
Can an unprivileged attacker batch message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths through a discovery peer advertising adversarial addresses and node records so `BlockUnclesVerifier` in `sync/src/relayer/block_uncles_verifier.rs` handles the first item safely but applies incorrect assumptions to later items and make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse, violating peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `sync/src/relayer/block_uncles_verifier.rs::BlockUnclesVerifier`
- Entrypoint: a discovery peer advertising adversarial addresses and node records
- Attacker controls: message ordering, retry timing, duplicate announcements, partial payloads, and boundary vector lengths
- Exploit idea: make peer-store, ban, or discovery state degrade across restart and enable repeated cheap abuse
- Invariant to test: peer scoring, ban, discovery, and reconnection state must remain robust against adversarial inputs
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
