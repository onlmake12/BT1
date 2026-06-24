The code is confirmed. Let me verify the exact behavior of `prune` to complete the impact analysis.

Audit Report

## Title
`send_unverified_block()` Compares Against Verified Tip Instead of Unverified Tip, Allowing `unverified_tip` High-Water Mark to Decrease — (`chain/src/orphan_broker.rs`)

## Summary

In `chain/src/orphan_broker.rs`, `send_unverified_block()` guards the `set_unverified_tip` call with `block_number > self.shared.snapshot().tip_number()` (the verified tip) rather than `block_number > self.shared.get_unverified_tip().number()` (the current unverified tip). Because `set_unverified_tip` is an unconditional atomic store with no monotonicity guard, any valid block delivered at a height above the verified tip but below the current `unverified_tip` silently decreases the high-water mark. This corrupts the three downstream consumers of `unverified_tip` in the synchronizer: the download-window gate, the IBD peer-skip check, and the `mark_slow_block` / `prune` inflight-tracking logic, degrading IBD throughput and causing incorrect peer scoring.

## Finding Description

**Root cause — `orphan_broker.rs` line 180:**

```rust
if block_number > self.shared.snapshot().tip_number() {   // ← verified tip, not unverified tip
    self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
        block_number,
        block_hash.clone(),
        U256::from(0u64),
    ));
``` [1](#0-0) 

`set_unverified_tip` performs an unconditional atomic store:

```rust
pub fn set_unverified_tip(&self, header: crate::HeaderIndex) {
    self.unverified_tip.store(Arc::new(header));
}
``` [2](#0-1) 

**Exploit path:**
1. Verified tip = 100; `unverified_tip` = 500 (blocks 101–500 queued for async verification).
2. A sync peer delivers valid block 200 whose parent (199) is `BLOCK_STORED` or `is_pending_verify`.
3. `OrphanBroker::process_descendant(block_200)` → `send_unverified_block(block_200)`.
4. Condition: `200 > snapshot().tip_number()` → `200 > 100` → **true**.
5. `set_unverified_tip(200)` executes — `unverified_tip` drops from **500 → 200**.

**Why existing checks fail:** There is no compare-and-swap or max-check before the store. The only other intentional decrease of `unverified_tip` is in `verify.rs` on block verification failure (line 167), which is a deliberate error-recovery reset — not a guard against the bug above. [3](#0-2) 

**Downstream corruption:**

*Download-window gate* (`block_fetcher.rs` line 122): `unverified_tip_limit = verified_tip + BLOCK_DOWNLOAD_WINDOW * 9`. Decreasing `unverified_tip` from 500 to 200 makes the gate pass again, re-opening the download window and issuing redundant block requests for blocks already queued. [4](#0-3) 

*IBD peer-skip check* (`block_fetcher.rs` line 191): Peers with `best_known` in [201, 500] are no longer skipped, causing duplicate inflight entries and degraded IBD throughput. [5](#0-4) 

*`mark_slow_block` / `prune`* (`block_fetcher.rs` line 307, `mod.rs` line 748): `mark_slow_block(200)` marks all inflight blocks with `key.number ≤ 201` as slow, triggering the penalty/re-request mechanism for blocks that are not actually slow. `prune(200)` only scans the `tip + 20 = 220` range, leaving timed-out entries in [221, 500] unscanned. [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

## Impact Explanation

The concrete impact is degraded IBD performance on the affected node: redundant block requests, inflated inflight state, incorrect peer scoring, and potential unnecessary peer disconnections. The node continues to operate and eventually completes IBD; there is no crash, no consensus deviation, and no economic damage. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation

Triggering the bug requires only that a sync peer deliver a valid block at a height above the verified tip but below the current `unverified_tip`. During multi-peer IBD, blocks routinely arrive out of order; any peer that delivers a lower-numbered block after higher-numbered blocks have already been queued satisfies the condition. No PoW mining is required — the peer relays existing chain blocks. The condition is met in normal IBD operation, making accidental triggering likely and deliberate triggering trivial for any connected peer.

## Recommendation

Change the guard in `send_unverified_block` to compare against the current `unverified_tip` instead of the verified snapshot tip:

```rust
// chain/src/orphan_broker.rs
if block_number > self.shared.get_unverified_tip().number() {
    self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
        block_number,
        block_hash.clone(),
        U256::from(0u64),
    ));
    // metrics / debug log unchanged
}
```

This ensures `unverified_tip` is strictly non-decreasing during normal sync, matching the intended high-water mark semantics.

## Proof of Concept

**Minimal manual steps:**

1. Start a CKB node in IBD mode connected to two peers.
2. Allow peer A to deliver blocks 101–500 in order; observe `unverified_tip` = 500 via `sync_state` RPC.
3. Have peer B (or replay a captured message) deliver block 200 (valid PoW, parent 199 already `BLOCK_STORED`).
4. Observe `unverified_tip` drops to 200 via `sync_state` RPC (`unverified_tip_number` field).
5. Observe the download window re-opens and the node begins re-requesting blocks 201–500 from peers.

**Unit test plan:** In `sync/src/tests/inflight_blocks.rs` (or a new test in `chain/src/`), construct an `OrphanBroker` with a mock `Shared` where `snapshot().tip_number() = 100` and `get_unverified_tip().number() = 500`. Call `send_unverified_block` with a block at height 200. Assert that `get_unverified_tip().number()` remains 500 after the fix, and equals 200 (the bug) before the fix. [10](#0-9)

### Citations

**File:** chain/src/orphan_broker.rs (L180-185)
```rust
        if block_number > self.shared.snapshot().tip_number() {
            self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                block_number,
                block_hash.clone(),
                U256::from(0u64),
            ));
```

**File:** shared/src/shared.rs (L407-409)
```rust
    pub fn set_unverified_tip(&self, header: crate::HeaderIndex) {
        self.unverified_tip.store(Arc::new(header));
    }
```

**File:** chain/src/verify.rs (L167-171)
```rust
                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));
```

**File:** sync/src/synchronizer/block_fetcher.rs (L122-129)
```rust
        if self.sync_shared.shared().get_unverified_tip().number() >= unverified_tip_limit {
            trace!(
                "unverified_tip - tip > BLOCK_DOWNLOAD_WINDOW * 9, skip fetch, unverified_tip: {}, tip: {}",
                self.sync_shared.shared().get_unverified_tip().number(),
                self.sync_shared.active_chain().tip_number()
            );
            return None;
        }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L191-201)
```rust
        if matches!(self.ibd, IBDState::In)
            && best_known.number() <= self.active_chain.unverified_tip_number()
        {
            debug!(
                "In IBD mode, Peer {}'s best_known: {} is less or equal than unverified_tip : {}, won't request block from this peer",
                self.peer,
                best_known.number(),
                self.active_chain.unverified_tip_number()
            );
            return None;
        };
```

**File:** sync/src/synchronizer/block_fetcher.rs (L306-314)
```rust
        let unverified_tip = self.active_chain.unverified_tip_number();
        let should_mark = fetch.last().is_some_and(|header| {
            header.number().saturating_sub(CHECK_POINT_WINDOW) > unverified_tip
        });
        if should_mark {
            state
                .write_inflight_blocks()
                .mark_slow_block(unverified_tip);
        }
```

**File:** sync/src/synchronizer/mod.rs (L748-755)
```rust
        let unverified_tip = self.shared.active_chain().unverified_tip_number();

        let disconnect_list = {
            let mut list = self
                .shared()
                .state()
                .write_inflight_blocks()
                .prune(unverified_tip);
```

**File:** sync/src/types/mod.rs (L628-636)
```rust
    pub fn mark_slow_block(&mut self, tip: BlockNumber) {
        let now = ckb_systemtime::unix_time_as_millis();
        for key in self.inflight_states.keys() {
            if key.number > tip + 1 {
                break;
            }
            self.trace_number.entry(key.clone()).or_insert(now);
        }
    }
```

**File:** sync/src/types/mod.rs (L638-665)
```rust
    pub fn prune(&mut self, tip: BlockNumber) -> HashSet<PeerIndex> {
        let now = unix_time_as_millis();
        let mut disconnect_list = HashSet::new();
        // Since statistics are currently disturbed by the processing block time, when the number
        // of transactions increases, the node will be accidentally evicted.
        //
        // Especially on machines with poor CPU performance, the node connection will be frequently
        // disconnected due to statistics.
        //
        // In order to protect the decentralization of the network and ensure the survival of low-performance
        // nodes, the penalty mechanism will be closed when the number of download nodes is less than the number of protected nodes
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let adjustment = self.adjustment;

        let trace = &mut self.trace_number;
        let download_schedulers = &mut self.download_schedulers;
        let states = &mut self.inflight_states;

        let mut remove_key = Vec::new();
        // Since this is a btreemap, with the data already sorted,
        // we don't have to worry about missing points, and we don't need to
        // iterate through all the data each time, just check within tip + 20,
        // with the checkpoint marking possible blocking points, it's enough
        let end = tip + 20;
        for (key, value) in states.iter() {
            if key.number > end {
                break;
            }
```

**File:** sync/src/tests/inflight_blocks.rs (L90-115)
```rust
#[test]
fn inflight_blocks_timeout() {
    let _faketime_guard = ckb_systemtime::faketime();
    _faketime_guard.set_faketime(0);
    let mut inflight_blocks = InflightBlocks::default();
    inflight_blocks.protect_num = 0;

    assert!(inflight_blocks.insert(1.into(), (1, h256!("0x1").into()).into()));
    assert!(inflight_blocks.insert(1.into(), (2, h256!("0x2").into()).into()));
    assert!(inflight_blocks.insert(2.into(), (3, h256!("0x3").into()).into()));
    assert!(!inflight_blocks.insert(1.into(), (3, h256!("0x3").into()).into()));
    assert!(inflight_blocks.insert(1.into(), (4, h256!("0x4").into()).into()));
    assert!(inflight_blocks.insert(2.into(), (5, h256!("0x5").into()).into()));
    assert!(!inflight_blocks.insert(2.into(), (5, h256!("0x5").into()).into()));

    _faketime_guard.set_faketime(BLOCK_DOWNLOAD_TIMEOUT + 1);

    assert!(!inflight_blocks.insert(3.into(), (3, h256!("0x3").into()).into()));
    assert!(!inflight_blocks.insert(3.into(), (2, h256!("0x2").into()).into()));
    assert!(inflight_blocks.insert(4.into(), (6, h256!("0x6").into()).into()));
    assert!(inflight_blocks.insert(1.into(), (7, h256!("0x7").into()).into()));

    let peers = inflight_blocks.prune(0);
    assert_eq!(peers, HashSet::from_iter(vec![1.into()]));
    assert!(inflight_blocks.insert(3.into(), (2, h256!("0x2").into()).into()));
    assert!(inflight_blocks.insert(3.into(), (3, h256!("0x3").into()).into()));
```
