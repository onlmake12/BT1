# Q2690: Medium storage resource amplification in new

## Question
Can an unprivileged attacker repeatedly send small database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size through a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches to make `new` in `store/src/data_loader_wrapper.rs` amplify CPU, memory, storage, or bandwidth and lose, duplicate, or stale-cache cells/headers across snapshots and write batches, violating database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data, causing Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism?

## Target
- File/function: `store/src/data_loader_wrapper.rs::new`
- Entrypoint: a node receiving valid-looking blocks that stress snapshots, caches, freezer, and write batches
- Attacker controls: database column contents derived from accepted blocks, freezer boundaries, migration versions, and write-batch size
- Exploit idea: lose, duplicate, or stale-cache cells/headers across snapshots and write batches
- Invariant to test: database writes, freezer state, migrations, and snapshots must be atomic for security-relevant data
- Expected Immunefi impact: Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism
- Fast validation: Use a temp database with accepted blocks plus rollback/restart/replay; assert cell/header/index state matches canonical verification.
