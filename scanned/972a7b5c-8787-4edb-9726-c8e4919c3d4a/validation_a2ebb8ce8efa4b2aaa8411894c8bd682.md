## Vulnerability Analysis

Let me trace the exact code path and check each guard.

### Attack Path Trace

**Entry point**: P2P `SendBlock` → `asynchronous_process_remote_block` → `asynchronous_process_lonely_block` → `ChainService::asynchronous_process_block`

**Gate 1 — `non_contextual_verify`** (`chain_service.rs` lines 72–88):
Calls `BlockVerifier` (PoW check: block hash meets difficulty target *in the header itself*) and `NonContextualBlockTxsVerifier`. Crucially, block-number consistency (`block_number == parent_number + 1`) is a **contextual** check — it requires looking up the parent's number from the DB, which non-contextual verification cannot do. An attacker can set `compact_target` to the minimum (e.g., `0x207fffff`) and trivially mine a valid PoW solution. This gate does **not** prevent an arbitrary `block_number`.

**Gate 2 — `process_lonely_block` routing** (`orphan_broker.rs` lines 107–132): [1](#0-0) 

If `parent_hash = current_tip_hash`, the parent has `BLOCK_STORED` status, so the block goes directly to `process_descendant` → `send_unverified_block`. No orphan pool involved.

**Gate 3 — `send_unverified_block`** (`orphan_broker.rs` lines 158–197): [2](#0-1) 

The condition is `block_number > self.shared.snapshot().tip_number()` — the **verified** tip. There is **no check against the current `unverified_tip`**. Any block number above the verified tip unconditionally overwrites `unverified_tip`.

**Gate 4 — `set_unverified_tip`** (`shared/src/shared.rs` lines 407–409): [3](#0-2) 

A bare `ArcSwap::store`. No monotonicity enforcement whatsoever.

**Effect — `block_fetcher.fetch()`** (`block_fetcher.rs` lines 111–129): [4](#0-3) 

`BLOCK_DOWNLOAD_WINDOW = 8192` (`util/constant/src/sync.rs` line 54), so the cutoff is `tip + 73728`. If `get_unverified_tip().number() >= tip + 73728`, `fetch()` returns `None` for **all peers**. [5](#0-4) 

### Concrete Attack

1. Attacker connects via P2P.
2. Crafts a block: `parent_hash = current_tip_hash`, `block_number = tip + 73729`, `compact_target = 0x207fffff`, mines trivial PoW.
3. Sends via `SendBlock` P2P message.
4. `non_contextual_verify` passes (PoW valid for the stated target; block number not checked contextually).
5. `insert_block` stores it in the DB.
6. `process_lonely_block` routes it to `process_descendant` (parent is stored tip).
7. `send_unverified_block` calls `set_unverified_tip(tip + 73729)`.
8. Every subsequent call to `block_fetcher.fetch()` hits the `>= unverified_tip_limit` guard and returns `None`.
9. No blocks are fetched from any peer. Verified tip cannot advance. `unverified_tip_limit` stays fixed. **Deadlock.**

### Key Invariant Broken

`unverified_tip` is supposed to advance monotonically and only reflect blocks that are plausibly on the canonical chain. The check in `send_unverified_block` uses `snapshot().tip_number()` (verified tip) instead of `get_unverified_tip().number()` (current unverified tip), so a single crafted block can jump `unverified_tip` arbitrarily far ahead of the verified tip, permanently suppressing `block_fetcher` for all peers.

### Uncertainty

Whether the contextual verifier (in `chain/src/verify.rs`, which has 3 references to `unverified_tip`) resets `unverified_tip` downward upon failure is not visible from the available code. If it does not, the deadlock persists until node restart. If it does reset it, the window of disruption is bounded by the time the fake block spends in the `preload_unverified_tx` queue — but the attack can be repeated continuously.

---

### Title
Unguarded `set_unverified_tip` in `send_unverified_block` allows trivial sync halt via crafted block — (`chain/src/orphan_broker.rs`)

### Summary
`send_unverified_block` calls `set_unverified_tip` whenever `block_number > snapshot().tip_number()`, with no check against the current `unverified_tip`. An attacker can submit a single P2P block with an arbitrarily high `block_number`, a minimum difficulty target, and a trivially mined PoW. After passing non-contextual verification, this inflates `unverified_tip` past `tip + BLOCK_DOWNLOAD_WINDOW * 9`, causing `block_fetcher.fetch()` to return `None` for all peers indefinitely.

### Finding Description
- `send_unverified_block` (`orphan_broker.rs:180`): guard is `block_number > snapshot().tip_number()`, not `block_number > get_unverified_tip().number()`.
- `set_unverified_tip` (`shared.rs:407`): unconditional store, no monotonicity.
- `block_fetcher.fetch()` (`block_fetcher.rs:122`): returns `None` if `unverified_tip >= tip + BLOCK_DOWNLOAD_WINDOW * 9`.
- `non_contextual_verify` does not check block-number/parent-number consistency (contextual check), so `block_number` is attacker-controlled.

### Impact Explanation
During IBD, all block downloads halt. The verified tip cannot advance. The node is effectively frozen until restarted (or until the contextual verifier resets `unverified_tip`, which is not confirmed from available code). A single UDP-weight P2P message suffices.

### Likelihood Explanation
Trivially exploitable: no significant hashpower required (minimum difficulty target), no privileged access, reachable via standard P2P `SendBlock`. Repeatable after node restart.

### Recommendation
In `send_unverified_block`, replace:
```rust
if block_number > self.shared.snapshot().tip_number() {
```
with:
```rust
if block_number > self.shared.get_unverified_tip().number() {
```
Additionally, add a monotonicity guard in `set_unverified_tip` using a CAS loop on the `ArcSwap` to reject updates that would decrease the value.

### Proof of Concept
Submit one block via P2P with `parent_hash = tip_hash`, `block_number = tip + BLOCK_DOWNLOAD_WINDOW * 9 + 1`, `compact_target = 0x207fffff`, valid trivial PoW. Assert `get_unverified_tip().number() >= tip + BLOCK_DOWNLOAD_WINDOW * 9`. Assert `block_fetcher.fetch()` returns `None`. Assert no further blocks are downloaded from any peer.

### Citations

**File:** chain/src/orphan_broker.rs (L107-125)
```rust
    pub(crate) fn process_lonely_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();
        let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
        let parent_status = self.shared.get_block_status(&parent_hash);
        if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
            debug!(
                "parent {} has stored: {:?} or is_pending_verify: {}, processing descendant directly {}-{}",
                parent_hash, parent_status, parent_is_pending_verify, block_number, block_hash,
            );
            self.process_descendant(lonely_block);
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }

        self.search_orphan_leaders();
```

**File:** chain/src/orphan_broker.rs (L180-196)
```rust
        if block_number > self.shared.snapshot().tip_number() {
            self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                block_number,
                block_hash.clone(),
                U256::from(0u64),
            ));

            if let Some(handle) = ckb_metrics::handle() {
                handle.ckb_chain_unverified_tip.set(block_number as i64);
            }
            debug!(
                "set unverified_tip to {}-{}, while unverified_tip - verified_tip = {}",
                block_number.clone(),
                block_hash,
                block_number.saturating_sub(self.shared.snapshot().tip_number())
            )
        }
```

**File:** shared/src/shared.rs (L407-409)
```rust
    pub fn set_unverified_tip(&self, header: crate::HeaderIndex) {
        self.unverified_tip.store(Arc::new(header));
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L111-129)
```rust
        let Some(unverified_tip_limit) = self
            .sync_shared
            .active_chain()
            .tip_number()
            .checked_add(BLOCK_DOWNLOAD_WINDOW * 9)
        else {
            trace!(
                "active chain tip is too close to BlockNumber::MAX to calculate unverified tip limit"
            );
            return None;
        };
        if self.sync_shared.shared().get_unverified_tip().number() >= unverified_tip_limit {
            trace!(
                "unverified_tip - tip > BLOCK_DOWNLOAD_WINDOW * 9, skip fetch, unverified_tip: {}, tip: {}",
                self.sync_shared.shared().get_unverified_tip().number(),
                self.sync_shared.active_chain().tip_number()
            );
            return None;
        }
```

**File:** util/constant/src/sync.rs (L54-54)
```rust
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```
