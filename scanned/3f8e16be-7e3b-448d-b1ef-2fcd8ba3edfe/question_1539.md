# Q1539: Critical network limit off by one in clear_expires

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs through a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks so `clear_expires` in `network/src/peer_store/ban_list.rs` desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply, violating malformed peer data must not crash a node or create low-cost network congestion, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `network/src/peer_store/ban_list.rs::clear_expires`
- Entrypoint: a sync peer delivering oversized, reordered, or inconsistent headers/blocks/compact blocks
- Attacker controls: compressed frame flags, length prefixes, snappy payloads, Molecule message bytes, and protocol IDs
- Exploit idea: desynchronize relay or sync state so valid blocks/transactions stop propagating cheaply
- Invariant to test: malformed peer data must not crash a node or create low-cost network congestion
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Use a protocol/codec unit test or fuzz harness with attacker-controlled bytes and assert bounded work, no panic, and expected peer punishment.
