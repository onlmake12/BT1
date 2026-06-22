### Title
Stale `HEADER_VALID` Status After `insert_block` Failure Due to Uncleared `header_map` Entry — (`chain/src/chain_service.rs`)

---

### Summary

When `insert_block` fails inside `asynchronous_process_block`, the code removes the block's entry from `block_status_map` but does **not** remove the corresponding entry from `header_map`. Because `get_block_status` falls back to `header_map` when `block_status_map` has no entry, the block is permanently misreported as `HEADER_VALID` — the same status as a block whose header was validated but whose body has never been received. This causes the sync state machine to treat a block that failed contextual insertion as one that still needs to be fetched, enabling repeated re-request and re-processing of the same invalid block.

---

### Finding Description

`asynchronous_process_block` in `chain/src/chain_service.rs` handles the failure path of `insert_block` as follows:

```rust
if let Err(err) = self.insert_block(&lonely_block) {
    error!(
        "insert block {}-{} failed: {:?}",
        block_number, block_hash, err
    );
    self.shared.block_status_map().remove(&block_hash);   // ← only this map is cleared
    lonely_block.execute_callback(Err(err));
    return;
}
``` [1](#0-0) 

The `block_status_map` entry is removed, but the `header_map` entry — populated earlier during header-first synchronization — is left intact.

`get_block_status` in `shared/src/shared.rs` performs a two-level lookup:

```rust
pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
    match self.block_status_map().get(block_hash) {
        Some(status_ref) => *status_ref.value(),
        None => {
            if self.header_map().contains_key(block_hash) {
                BlockStatus::HEADER_VALID          // ← stale fallback
            } else {
                // … database lookup …
            }
        }
    }
}
``` [2](#0-1) 

After the removal, `block_status_map` has no entry for the block, so the function falls through to the `header_map` check. Because the header was previously stored there during header sync, the function returns `HEADER_VALID` — exactly the status used to signal "header known, body not yet received." The block's actual outcome (insertion failure) is invisible to every caller of `get_block_status`.

The contrast with the non-contextual failure path makes the inconsistency explicit: that path correctly sets `BLOCK_INVALID` before returning:

```rust
self.shared
    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
``` [3](#0-2) 

The `insert_block` failure path does the opposite — it removes the status entirely, leaving the `header_map` as the sole source of truth, which reports a stale `HEADER_VALID`.

The `header_map` and `block_status_map` are explicitly documented as requiring synchronized updates:

> "Never use `LruCache` as container. We have to ensure synchronizing between `orphan_block_pool` and `block_status_map`" [4](#0-3) 

The same synchronization requirement applies between `header_map` and `block_status_map`, but it is not enforced in the `insert_block` failure path.

---

### Impact Explanation

Any component that calls `get_block_status` to gate block processing will observe `HEADER_VALID` for a block that actually failed contextual insertion. Concretely:

- The **sync block fetcher** (`block_fetcher.rs`) uses `get_block_status` to decide which blocks to request. A block returning `HEADER_VALID` is treated as "header known, body needed" and will be re-requested from peers indefinitely.
- The **relay compact-block processor** uses `get_block_status` to skip already-stored or already-invalid blocks. A block returning `HEADER_VALID` bypasses those guards and is re-processed.
- The **orphan broker** (`orphan_broker.rs`) checks `get_block_status` to decide whether to propagate orphan descendants. A stale `HEADER_VALID` for a failed block could cause orphan descendants to be forwarded for processing even though their ancestor is invalid.

The net effect is that a block that failed contextual verification is silently re-queued for download and re-processing on every sync cycle, consuming CPU, I/O, and network bandwidth proportional to the number of such blocks an attacker can inject.

---

### Likelihood Explanation

The precondition is that a block's header must already be in `header_map` (populated during header-first sync) when its body arrives and fails `insert_block`. This is the normal operating sequence during IBD and steady-state sync: headers arrive first via `GetHeaders`/`SendHeaders`, are stored in `header_map`, and block bodies are fetched afterward. Any peer that can relay a syntactically valid block (passing non-contextual checks) whose contextual verification fails — e.g., a block with an invalid transaction, wrong difficulty, or bad cellbase — can trigger this path. No privileged access is required; an unprivileged P2P peer is sufficient.

---

### Recommendation

In the `insert_block` failure branch of `asynchronous_process_block`, explicitly set the block status to `BLOCK_INVALID` **and** remove the `header_map` entry, mirroring the non-contextual failure path:

```rust
if let Err(err) = self.insert_block(&lonely_block) {
    error!("insert block {}-{} failed: {:?}", block_number, block_hash, err);
    self.shared
        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
    self.shared.remove_header_view(&block_hash);   // clear stale header_map entry
    lonely_block.execute_callback(Err(err));
    return;
}
```

This ensures `get_block_status` returns `BLOCK_INVALID` rather than falling back to the stale `HEADER_VALID` from `header_map`, consistent with how non-contextual failures are handled at line 126–128.

---

### Proof of Concept

**Attacker-controlled entry path:** An unprivileged P2P peer acting as a block relayer.

**Step-by-step:**

1. The attacker connects to a CKB node as a sync peer.
2. The attacker sends a sequence of block headers via `SendHeaders`. The node validates the headers and stores them in `header_map` with status `HEADER_VALID`.
3. The node issues `GetBlocks` for the corresponding block bodies.
4. The attacker responds with a block body that passes non-contextual verification (`BlockVerifier` + `NonContextualBlockTxsVerifier`) but fails contextual insertion — for example, a block containing a transaction that double-spends a live cell, or a block with an incorrect epoch difficulty.
5. `asynchronous_process_block` calls `insert_block`, which returns `Err`. The code at line 138 removes the `block_status_map` entry but leaves the `header_map` entry intact.
6. On the next sync timer tick, `find_blocks_to_fetch` calls `get_block_status` for this block hash. `block_status_map` has no entry; `header_map` still has the header → returns `HEADER_VALID`. The block is re-added to the fetch queue.
7. The node re-requests the block body from the same or another peer. If the attacker (or a colluding peer) serves the same body, step 4–6 repeats indefinitely.
8. Each cycle burns CPU (contextual verification), disk I/O (partial write attempt), and network bandwidth (block body download), constituting a sustained resource-exhaustion condition triggered by a single unprivileged peer.

### Citations

**File:** chain/src/chain_service.rs (L126-128)
```rust
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                lonely_block.execute_callback(Err(err));
```

**File:** chain/src/chain_service.rs (L133-141)
```rust
        if let Err(err) = self.insert_block(&lonely_block) {
            error!(
                "insert block {}-{} failed: {:?}",
                block_number, block_hash, err
            );
            self.shared.block_status_map().remove(&block_hash);
            lonely_block.execute_callback(Err(err));
            return;
        }
```

**File:** shared/src/shared.rs (L425-445)
```rust
    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
                }
            }
        }
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L125-127)
```rust
// NOTE: Never use `LruCache` as container. We have to ensure synchronizing between
// orphan_block_pool and block_status_map, but `LruCache` would prune old items implicitly.
// RwLock ensures the consistency between maps. Using multiple concurrent maps does not work here.
```
