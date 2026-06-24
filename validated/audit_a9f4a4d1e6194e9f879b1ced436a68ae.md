All seven code references were verified against the actual codebase. Here is the validation result:

---

Audit Report

## Title
Orphan-parent invalidation incorrectly bans the innocent sender of a child block — (`chain/src/orphan_broker.rs`, `sync/src/synchronizer/block_process.rs`)

## Summary
When block B enters the orphan pool, its `verify_callback` permanently captures the `PeerIndex` of B's sender. If a different peer later causes B's parent P to be marked `BLOCK_INVALID`, `OrphanBroker::process_invalid_block` fires B's callback with `InternalErrorKind::Other`. Because `is_internal_db_error` returns `false` for `Other`, the guard is not taken and `post_sync_process` bans the peer that sent B — even though B itself may be structurally valid and its sender is innocent.

## Finding Description

**Step 1 — Block B enters the orphan pool.**
`BlockProcess::execute` constructs a `verify_callback` that closes over `peer_id` (B's sender) and `nc`. [1](#0-0) 
Because P's status is neither `BLOCK_STORED` nor `BLOCK_INVALID`, B is inserted into the orphan pool. [2](#0-1) 

**Step 2 — Parent P is invalidated.**
When any block whose parent is already `BLOCK_INVALID` arrives, `process_lonely_block` calls `process_invalid_block` on it, marking it `BLOCK_INVALID`, then calls `search_orphan_leaders()`. [3](#0-2) 
`search_orphan_leader` detects P as `BLOCK_INVALID` and calls `process_invalid_block` on every orphan child of P (including B). [4](#0-3) 

**Step 3 — `process_invalid_block` fires B's callback with `InternalErrorKind::Other`.** [5](#0-4) 

**Step 4 — `is_internal_db_error` returns `false` for `Other`.**
The guard only passes for `Database` and `System`; `Other` is not covered, so the early return is not taken. [6](#0-5) 

**Step 5 — `post_sync_process` bans peer B.**
`StatusCode::BlockIsInvalid` falls in the `400..500` range, so `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)`. [7](#0-6) 
`post_sync_process` then calls `nc.ban_peer(peer_id_of_B, ban_time, ...)`. [8](#0-7) 

## Impact Explanation
An unprivileged attacker with a single P2P connection can selectively and permanently (for `BAD_MESSAGE_BAN_TIME`) ban any honest peer from the victim node. By repeating this against multiple honest peers, the attacker can progressively isolate the victim from its honest peer set, degrading its ability to receive valid blocks and transactions. This constitutes a targeted peer-isolation/eclipse-attack primitive achievable at negligible cost. **High — Vulnerabilities or bad designs which could cause CKB network congestion/isolation with few costs.**

## Likelihood Explanation
The precondition — a block sitting in the orphan pool — is routine during IBD and out-of-order block arrival. The attacker needs only one P2P connection to the victim and knowledge of one block hash in the orphan pool (observable via timing or network monitoring). No hashpower, keys, or privileged access are required. The attack is repeatable and cheap.

## Recommendation
In `process_invalid_block` (`orphan_broker.rs`), do not fire the `verify_callback` with a banning-eligible error. Concrete options:
- Fire the callback with `Ok(false)` (already-verified sentinel) instead of `Err(...)`.
- Use `InternalErrorKind::System` (which `is_internal_db_error` returns `true` for) so the callback's guard suppresses the ban.
- Add a dedicated `InternalErrorKind::ParentInvalid` variant and extend `is_internal_db_error` (or a new predicate) to suppress banning for it, making the intent explicit.

## Proof of Concept
```
Victim node V, Peer A (attacker), Peer B (honest)

1. Peer B sends block B (parent = P, P not stored on V).
   → V: orphan_pool.insert(B, callback=ban(peer_B))

2. Peer A sends block P' where P' is a child of an already-BLOCK_INVALID block.
   → V: process_lonely_block(P') → process_invalid_block(P') → insert_block_status(P, BLOCK_INVALID)
   → V: search_orphan_leaders() finds P as leader with BLOCK_INVALID
   → V: process_invalid_block(B)
        fires callback(Err(InternalErrorKind::Other("parent P is invalid...")))
        is_internal_db_error → false
        post_sync_process(peer_B, BlockIsInvalid)
        should_ban() → Some(BAD_MESSAGE_BAN_TIME)
        nc.ban_peer(peer_B, BAD_MESSAGE_BAN_TIME)

3. Peer B is now banned from V despite having sent a valid block.
```
A unit test can be written in `sync/src/synchronizer/` that constructs a mock `CKBProtocolContext`, inserts a block into the orphan pool via `BlockProcess::execute`, then calls `process_invalid_block` directly and asserts that `ban_peer` is called on the original sender's `PeerIndex`.

### Citations

**File:** sync/src/synchronizer/block_process.rs (L44-70)
```rust
            let verify_callback = {
                let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&self.nc);
                let peer_id: PeerIndex = self.peer;
                let block_hash: Byte32 = block.hash();
                Box::new(move |verify_result: Result<bool, ckb_error::Error>| {
                    match verify_result {
                        Ok(_) => {}
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
                        }
                    };
                })
            };
```

**File:** chain/src/orphan_broker.rs (L39-50)
```rust
    fn search_orphan_leader(&self, leader_hash: ParentHash) {
        let leader_status = self.shared.get_block_status(&leader_hash);

        if leader_status.eq(&BlockStatus::BLOCK_INVALID) {
            let descendants: Vec<LonelyBlockHash> = self
                .orphan_blocks_broker
                .remove_blocks_by_parent(&leader_hash);
            for descendant in descendants {
                self.process_invalid_block(descendant);
            }
            return;
        }
```

**File:** chain/src/orphan_broker.rs (L98-104)
```rust
        let err: VerifyResult = Err(InternalErrorKind::Other
            .other(format!(
                "parent {} is invalid, so block {}-{} is invalid too",
                parent_hash, block_number, block_hash
            ))
            .into());
        lonely_block.execute_callback(err);
```

**File:** chain/src/orphan_broker.rs (L119-125)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }

        self.search_orphan_leaders();
```

**File:** error/src/lib.rs (L109-112)
```rust
        } else {
            return error_kind == InternalErrorKind::Database
                || error_kind == InternalErrorKind::System;
        }
```

**File:** sync/src/status.rs (L165-179)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```

**File:** sync/src/types/mod.rs (L2008-2013)
```rust
    if let Some(ban_time) = status.should_ban() {
        error!(
            "Receive {} from {}. Ban {:?} for {}",
            item_name, peer, ban_time, status
        );
        nc.ban_peer(peer, ban_time, status.to_string());
```
