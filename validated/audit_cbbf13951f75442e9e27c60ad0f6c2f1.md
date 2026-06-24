Audit Report

## Title
Orphan-parent invalidation incorrectly bans the innocent sender of a child block — (`chain/src/orphan_broker.rs`, `sync/src/synchronizer/block_process.rs`)

## Summary

When block B is placed in the orphan pool, its `verify_callback` permanently captures the `PeerIndex` of whoever sent B. If a different peer later causes B's parent P to be marked `BLOCK_INVALID`, `OrphanBroker::process_invalid_block` fires B's callback with an `InternalErrorKind::Other` error. Because `is_internal_db_error` returns `false` for `Other`, the callback unconditionally calls `post_sync_process`, which bans the peer that sent B — even though B itself may be structurally valid and its sender is innocent.

## Finding Description

**Step 1 — Block B enters the orphan pool with a captured peer identity.**

In `BlockProcess::execute`, a `verify_callback` closure is constructed that permanently captures `peer_id` (the sender of B): [1](#0-0) 

Because P is not yet stored and not invalid, B is inserted into the orphan pool: [2](#0-1) 

**Step 2 — Parent P is invalidated by a different peer; orphan scan fires.**

After every `process_lonely_block` call, `search_orphan_leaders()` is invoked: [3](#0-2) 

`search_orphan_leader` detects `BLOCK_INVALID` on P and calls `process_invalid_block` on every orphan child of P: [4](#0-3) 

**Step 3 — `process_invalid_block` fires B's callback with `InternalErrorKind::Other`.** [5](#0-4) 

The error kind is `Other`, not `Database` or `System`.

**Step 4 — `is_internal_db_error` returns `false` for `Other`, so the guard is not taken.** [6](#0-5) 

The early-return at `block_process.rs:53-55` is skipped, and execution falls through to `post_sync_process`. [7](#0-6) 

**Step 5 — `post_sync_process` bans the innocent peer.**

`StatusCode::BlockIsInvalid` falls in the `400..500` range, so `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)`: [8](#0-7) 

`post_sync_process` then calls `nc.ban_peer(peer_id_of_B, ban_time, ...)`: [9](#0-8) 

## Impact Explanation

An unprivileged attacker with a single P2P connection to the victim can selectively and repeatedly ban any honest peer from the victim node. By sending an invalid version of a known orphan's parent, the attacker causes the victim to permanently sever its connection to the honest peer for `BAD_MESSAGE_BAN_TIME`. Repeated application across multiple peers can progressively isolate the victim node from its honest peer set, enabling eclipse-attack preconditions or targeted network partitioning. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion/isolation with few costs*, as the attacker expends only one connection and one invalid block message per ban.

## Likelihood Explanation

The precondition — a block in the orphan pool — is routine during IBD and any out-of-order block arrival. The orphan pool contents are partially observable via timing side-channels or network monitoring. The attacker requires no hashpower, no keys, and no privileged access. The attack is repeatable: after banning one peer, the attacker can target the next orphan entry. The cost per ban is a single malformed block message.

## Recommendation

In `process_invalid_block` (`chain/src/orphan_broker.rs`), do not fire the `verify_callback` with a banning-eligible error. Concrete options:

- Fire the callback with `Ok(false)` (already-verified sentinel) so the callback's `Ok(_)` arm is taken and no ban occurs.
- Use `InternalErrorKind::System` instead of `InternalErrorKind::Other`, which `is_internal_db_error` already returns `true` for, causing the callback to early-return.
- Introduce a dedicated `InternalErrorKind::ParentInvalid` variant and extend `is_internal_db_error` (or a new predicate) to suppress banning for it, making the intent explicit.

## Proof of Concept

```
Victim node V, Peer A (attacker), Peer B (honest)

1. Peer B sends block B (parent = P, P not stored on V).
   → V: orphan_pool.insert(B, callback=ban(peer_B))

2. Peer A sends block P' with an invalid field (e.g., bad PoW).
   → V: non_contextual_verify(P') fails
   → V: insert_block_status(P, BLOCK_INVALID)
   → V: search_orphan_leaders() finds P as leader
   → V: process_invalid_block(B)
        fires callback(Err(InternalErrorKind::Other("parent P is invalid...")))
        is_internal_db_error(Other) → false
        post_sync_process(peer_B, BlockIsInvalid=401)
        should_ban() → Some(BAD_MESSAGE_BAN_TIME)
        nc.ban_peer(peer_B, BAD_MESSAGE_BAN_TIME)

3. Peer B is now banned from V despite having sent a structurally valid block.
```

A targeted integration test can confirm this by: constructing two peers, having peer B submit an orphan block, having peer A submit an invalid parent, and asserting that peer B's connection is banned while peer A's is not.

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

**File:** chain/src/orphan_broker.rs (L125-125)
```rust
        self.search_orphan_leaders();
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
