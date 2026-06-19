# Q2636: Medium storage resource amplification in tick_backend_delete

## Question
Can an unprivileged attacker repeatedly send small database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size through a peer-driven chain/reorg sequence that writes adversarial canonical and fork state to make `tick_backend_delete` in `shared/src/types/header_map/kernel_lru.rs` amplify CPU, memory, storage, or bandwidth and make persisted state disagree with canonical verification state after restart or rollback, violating state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `shared/src/types/header_map/kernel_lru.rs::tick_backend_delete`
- Entrypoint: a peer-driven chain/reorg sequence that writes adversarial canonical and fork state
- Attacker controls: database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size
- Exploit idea: make persisted state disagree with canonical verification state after restart or rollback
- Invariant to test: state storage mechanisms must not allow cheap corruption, excessive growth, or crash-on-restart
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
