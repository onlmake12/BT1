All code references have been verified against the actual codebase. The exploit path is confirmed real:

1. `block_process.rs` L44-70: `verify_callback` closes over `peer_id` of block B's sender. [1](#0-0) 
2. `orphan_broker.rs` L121-122: B is inserted into orphan pool when parent status is neither stored nor invalid. [2](#0-1) 
3. `orphan_broker.rs` L39-50: `search_orphan_leader` fires `process_invalid_block` on all orphan children when leader is `BLOCK_INVALID`. [3](#0-2) 
4. `orphan_broker.rs` L98-104: `process_invalid_block` fires callback with `InternalErrorKind::Other`. [4](#0-3) 
5. `error/src/lib.rs` L110-111: `is_internal_db_error` returns `true` only for `Database` and `System`, not `Other`. [5](#0-4) 
6. `status.rs` L165-179: `StatusCode::BlockIsInvalid = 401` is in `400..500`, so `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)`. [6](#0-5) 
7. `types/mod.rs` L2008-2013: `post_sync_process` calls `nc.ban_peer` on the captured `peer_id`. [7](#0-6) 

---

Audit Report

## Title
Orphan-parent invalidation incorrectly bans the innocent sender of a child block — (`chain/src/orphan_broker.rs`, `sync/src/synchronizer/block_process.rs`)

## Summary
When block B is placed in the orphan pool, its `verify_callback` permanently captures the `PeerIndex` of B's sender. If a different peer later causes B's parent P to be marked `BLOCK_INVALID`, `OrphanBroker::process_invalid_block` fires B's callback with `InternalErrorKind::Other`. Because `is_internal_db_error` returns `false` for `Other`, the guard is not taken and `post_sync_process` bans the peer that sent B — even though B itself may be structurally valid and its sender is innocent.

## Finding Description
**Step 1 — Block B enters the orphan pool.**
In `BlockProcess::execute` (`block_process.rs` L44-70), a `verify_callback` is constructed that closes over `peer_id` (the sender of B). Because P's status is neither `BLOCK_STORED` nor `BLOCK_INVALID`, B is inserted into the orphan pool (`orphan_broker.rs` L121-122).

**Step 2 — Parent P is invalidated by a different peer.**
At the end of every `process_lonely_block` call, `search_orphan_leaders()` is invoked (`orphan_broker.rs` L125). `search_orphan_leader` checks if the leader (P) is `BLOCK_INVALID` and, if so, calls `process_invalid_block` on every orphan child (`orphan_broker.rs` L42-49).

**Step 3 — `process_invalid_block` fires B's callback with `InternalErrorKind::Other`.**
`process_invalid_block` (`orphan_broker.rs` L98-104) constructs an error with `InternalErrorKind::Other` and calls `lonely_block.execute_callback(err)`.

**Step 4 — `is_internal_db_error` returns `false` for `Other`.**
`error/src/lib.rs` L110-111 only returns `true` for `Database` and `System`. The early-return guard in the callback (`block_process.rs` L52-55) is therefore not taken.

**Step 5 — `post_sync_process` bans peer B.**
`StatusCode::BlockIsInvalid = 401` falls in the `400..500` range, so `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` (`status.rs` L166-178). `post_sync_process` then calls `nc.ban_peer(peer_id_of_B, ban_time, ...)` (`types/mod.rs` L2013).

**Why existing checks fail:**
The `is_internal_db_error` guard was designed to suppress bans for transient DB/system errors, not for the "parent is invalid" semantic error. `InternalErrorKind::Other` is not covered by this guard, so the ban proceeds unconditionally.

## Impact Explanation
An unprivileged attacker with a single P2P connection to the victim can selectively and permanently (for `BAD_MESSAGE_BAN_TIME`) ban any honest peer from the victim node. By repeating this against multiple honest peers, the attacker can progressively isolate the victim from its honest peer set, degrading its ability to receive valid blocks and transactions. This constitutes a targeted peer-isolation/eclipse-attack primitive achievable at negligible cost, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion/isolation with few costs.**

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

2. Peer A sends block P with an invalid field (e.g., bad PoW).
   → V: non_contextual_verify(P) fails
   → V: insert_block_status(P, BLOCK_INVALID)
   → V: search_orphan_leaders() finds P as leader
   → V: process_invalid_block(B)
        fires callback(Err(InternalErrorKind::Other("parent P is invalid...")))
        is_internal_db_error → false
        post_sync_process(peer_B, BlockIsInvalid=401)
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

**File:** chain/src/orphan_broker.rs (L88-105)
```rust
    fn process_invalid_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();

        self.delete_block(&lonely_block);

        self.shared
            .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);

        let err: VerifyResult = Err(InternalErrorKind::Other
            .other(format!(
                "parent {} is invalid, so block {}-{} is invalid too",
                parent_hash, block_number, block_hash
            ))
            .into());
        lonely_block.execute_callback(err);
    }
```

**File:** chain/src/orphan_broker.rs (L119-123)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }
```

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

**File:** sync/src/types/mod.rs (L2002-2013)
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
```
