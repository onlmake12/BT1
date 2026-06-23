The call chain is fully traceable. Let me confirm the exact lines in `consume_unverified_blocks` that bridge `reconcile_main_chain` errors to the callback.

The entire call chain is now fully mapped. Here is the assessment:

---

### Title
`InternalErrorKind::MMR` Omitted from `is_internal_db_error` Guard Causes Honest Peer Ban on Local MMR Storage Failure — (`error/src/lib.rs`, `chain/src/verify.rs`)

### Summary

`is_internal_db_error` only exempts `InternalErrorKind::Database` and `InternalErrorKind::System` from triggering peer bans. `InternalErrorKind::MMR` — which wraps local MMR storage failures in `reconcile_main_chain` — is not included. When a local MMR node is missing or corrupt, any peer relaying a valid block that triggers `reconcile_main_chain` will be banned, even though the fault is entirely local.

### Finding Description

**`is_internal_db_error` definition** — `error/src/lib.rs` lines 101–114:

```rust
pub fn is_internal_db_error(error: &Error) -> bool {
    if error.kind() == ErrorKind::Internal {
        let error_kind = ...kind();
        if error_kind == InternalErrorKind::DataCorrupted { panic!(...) }
        else {
            return error_kind == InternalErrorKind::Database
                || error_kind == InternalErrorKind::System;  // MMR is absent
        }
    }
    false
}
``` [1](#0-0) 

`InternalErrorKind::MMR` is a distinct variant: [2](#0-1) 

**`reconcile_main_chain` emits `InternalErrorKind::MMR`** at three sites — `chain/src/verify.rs`:

- Line 634: `mmr.push(b.digest()).map_err(|e| InternalErrorKind::MMR.other(e))?;` (already-verified blocks)
- Line 684: same, for newly verified blocks
- Line 732: `mmr.commit().map_err(|e| InternalErrorKind::MMR.other(e))?;` [3](#0-2) [4](#0-3) 

**`reconcile_main_chain` is called from `verify_block`**, which is called from `consume_unverified_blocks`: [5](#0-4) 

**`consume_unverified_blocks` uses `is_internal_db_error` to decide whether to mark the block `BLOCK_INVALID` and fire the callback with the error:** [6](#0-5) 

Since `is_internal_db_error` returns `false` for `InternalErrorKind::MMR`, the block is stamped `BLOCK_INVALID` and the `verify_result = Err(MMR error)` is passed to the callback.

**Both the sync and relay callbacks check `is_internal_db_error` before banning:**

In `sync/src/synchronizer/block_process.rs`:
```rust
let is_internal_db_error = is_internal_db_error(&err);
if is_internal_db_error { return; }
// punish the malicious peer
post_sync_process(..., StatusCode::BlockIsInvalid...);
``` [7](#0-6) 

In `sync/src/relayer/mod.rs`: [8](#0-7) 

**`post_sync_process` calls `nc.ban_peer`:** [9](#0-8) 

### Impact Explanation

When the local node's MMR store has any inconsistency (missing node, incomplete migration, partial write after power failure), the next valid block relayed by any honest peer triggers `reconcile_main_chain` → `InternalErrorKind::MMR` → `is_internal_db_error == false` → `post_sync_process(BlockIsInvalid)` → `nc.ban_peer(peer, ban_time, ...)`. The honest peer is persistently banned. The block is also permanently stamped `BLOCK_INVALID` in the local block status map, poisoning future sync for that block hash.

### Likelihood Explanation

The pre-condition — a local MMR inconsistency — is not attacker-controlled, which limits exploitability. However, it is realistic: the MMR was added via a database migration (`add_chain_root_mmr`); a partial migration, power failure during `mmr.commit()`, or RocksDB column-family corruption affecting only the MMR column can produce exactly this state. Once the node is in this state, any honest peer relaying the next best-chain block triggers the ban. The attacker does not need to do anything beyond normal block relay.

### Recommendation

Add `InternalErrorKind::MMR` to the exemption list in `is_internal_db_error`:

```rust
return error_kind == InternalErrorKind::Database
    || error_kind == InternalErrorKind::System
    || error_kind == InternalErrorKind::MMR;  // MMR is a local storage error
``` [10](#0-9) 

### Proof of Concept

1. Start a CKB node and let it sync to height N with a valid MMR.
2. Directly delete or corrupt one MMR node entry in the RocksDB MMR column family (simulating a partial migration or disk fault).
3. Connect a peer and have it relay a valid block at height N+1.
4. Observe: `reconcile_main_chain` fails with `InternalErrorKind::MMR`; `is_internal_db_error` returns `false`; `post_sync_process(BlockIsInvalid)` fires; the peer is banned.
5. Confirm with a unit test: `assert!(!is_internal_db_error(&InternalErrorKind::MMR.other("test").into()))` — this assertion passes, proving the guard is absent.

### Citations

**File:** error/src/lib.rs (L101-114)
```rust
pub fn is_internal_db_error(error: &Error) -> bool {
    if error.kind() == ErrorKind::Internal {
        let error_kind = error
            .downcast_ref::<InternalError>()
            .expect("error kind checked")
            .kind();
        if error_kind == InternalErrorKind::DataCorrupted {
            panic!("{}", error)
        } else {
            return error_kind == InternalErrorKind::Database
                || error_kind == InternalErrorKind::System;
        }
    }
    false
```

**File:** error/src/internal.rs (L44-46)
```rust
    /// MMR internal error
    MMR,

```

**File:** chain/src/verify.rs (L175-197)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }

                error!(
                    "set_unverified tip to {}-{}, because verify {} failed: {}",
                    tip.number(),
                    tip.hash(),
                    block_hash,
                    err
                );
            }
        }

        self.is_pending_verify.remove(&block_hash);

        if let Some(callback) = verify_callback {
            callback(verify_result);
        }
```

**File:** chain/src/verify.rs (L344-347)
```rust
            // MUST update index before reconcile_main_chain
            let begin_reconcile_main_chain = std::time::Instant::now();
            self.reconcile_main_chain(Arc::clone(&db_txn), &mut fork, switch)?;
            trace!(
```

**File:** chain/src/verify.rs (L630-635)
```rust
        for b in fork.attached_blocks().iter().take(verified_len) {
            txn.attach_block(b)?;
            attach_block_cell(&txn, b)?;
            mmr.push(b.digest())
                .map_err(|e| InternalErrorKind::MMR.other(e))?;
        }
```

**File:** chain/src/verify.rs (L730-733)
```rust
            trace!("light-client: commit");
            // Before commit, all new MMR nodes are in memory only.
            mmr.commit().map_err(|e| InternalErrorKind::MMR.other(e))?;
            Ok(())
```

**File:** sync/src/synchronizer/block_process.rs (L51-66)
```rust
                        Err(err) => {
                            let is_internal_db_error = is_internal_db_error(&err);
                            if is_internal_db_error {
                                return;
                            }

                            // punish the malicious peer
                            post_sync_process(
                                nc.as_ref(),
                                peer_id,
                                "SendBlock",
                                StatusCode::BlockIsInvalid.with_context(format!(
                                    "block {} is invalid, reason: {}",
                                    block_hash, err
                                )),
                            );
```

**File:** sync/src/relayer/mod.rs (L317-332)
```rust
                    let is_internal_db_error = is_internal_db_error(&err);
                    if is_internal_db_error {
                        return;
                    }

                    // punish the malicious peer
                    post_sync_process(
                        nc.as_ref(),
                        peer_id,
                        &msg_name,
                        StatusCode::BlockIsInvalid.with_context(format!(
                            "block {} is invalid, reason: {}",
                            block.hash(),
                            err
                        )),
                    );
```

**File:** sync/src/types/mod.rs (L2002-2014)
```rust
pub(crate) fn post_sync_process(
    nc: &dyn CKBProtocolContext,
    peer: PeerIndex,
    item_name: &str,
    status: Status,
) {
    if let Some(ban_time) = status.should_ban() {
        error!(
            "Receive {} from {}. Ban {:?} for {}",
            item_name, peer, ban_time, status
        );
        nc.ban_peer(peer, ban_time, status.to_string());
    } else if status.should_warn() {
```
