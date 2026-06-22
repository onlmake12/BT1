### Title
Off-by-One in Compact Block Staleness Check Allows Boundary-Height Blocks to Bypass Early Rejection — (`File: sync/src/relayer/compact_block_process.rs`)

### Summary
The `non_contextual_check` function in the compact block relay path uses a strict `>` comparison instead of `>=` when checking whether a relayed compact block is too stale to process. This allows a peer to relay a compact block whose height equals exactly `tip - epoch_length` — the intended rejection boundary — causing the node to perform unnecessary downstream validation work that should have been short-circuited immediately.

### Finding Description

In `sync/src/relayer/compact_block_process.rs`, the `non_contextual_check` function computes a staleness lower bound and checks it as follows:

```rust
// Only accept blocks with a height greater than tip - N
// where N is the current epoch length
let block_hash = header.hash();
let tip = active_chain.tip_header();
let epoch_length = active_chain.epoch_ext().length();
let lowest_number = tip.number().saturating_sub(epoch_length);

if lowest_number > header.number() {
    return StatusCode::CompactBlockIsStaled.with_context(block_hash);
}
``` [1](#0-0) 

The inline comment explicitly states the intended invariant: **"Only accept blocks with a height greater than tip - N"** — meaning `header.number() > lowest_number`, so the boundary value `header.number() == lowest_number` should be rejected.

However, the guard condition `lowest_number > header.number()` only fires when `header.number() < lowest_number`. When `header.number() == lowest_number` (exactly at the boundary), the condition is `false` and the block is **not** rejected — it passes through to the more expensive `contextual_check` path.

The fix is to change `>` to `>=`:

```rust
if lowest_number >= header.number() {
    return StatusCode::CompactBlockIsStaled.with_context(block_hash);
}
```

This is structurally identical to the OCL-3 pattern: a guard that should use `>=` uses `>`, allowing the boundary value to slip through.

### Impact Explanation

Any peer can craft and relay a `CompactBlock` message whose header number equals exactly `tip.number() - epoch_length`. Because the staleness guard is bypassed for this one height, the node proceeds into `contextual_check`: [2](#0-1) 

This involves database lookups (`get_block_status`, `get_header_index_view`), peer-state mutations (`may_set_best_known_header`), and potentially further header verification work — all of which should have been avoided by the early-exit staleness guard. An adversarial peer can repeatedly send such messages (one per tip advance) to impose a sustained, amplified per-message processing cost on the victim node, constituting a targeted resource-exhaustion vector.

### Likelihood Explanation

The entry path is fully reachable by any unprivileged P2P peer: sending a `CompactBlock` message is a standard part of the CKB relay protocol. No special role, key, or majority hashpower is required. The attacker only needs to know the current tip number (trivially obtained via the sync protocol) and set `header.number = tip - epoch_length`. The condition is deterministic and reproducible on every block.

### Recommendation

Change the strict inequality to a non-strict one in `non_contextual_check`:

```rust
// Before
if lowest_number > header.number() {

// After
if lowest_number >= header.number() {
``` [3](#0-2) 

This aligns the code with the documented invariant ("height **greater than** tip - N") and closes the one-block gap at the staleness boundary.

### Proof of Concept

1. Observe the current tip number `T` and epoch length `L` (both available via the sync protocol).
2. Construct a `CompactBlock` message with `header.number = T - L`.
3. Send it to a target node over the P2P relay protocol.
4. The guard `lowest_number > header.number()` evaluates as `(T - L) > (T - L)` → `false`, so `CompactBlockIsStaled` is **not** returned.
5. The node enters `contextual_check`, performing database reads and peer-state writes for a block that the comment explicitly designates as stale.
6. Repeat on every new block to sustain the amplified processing load. [4](#0-3)

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L188-223)
```rust
/// * check compact block's uncles and proposals length
/// * check compact block height
fn non_contextual_check(
    compact_block: &CompactBlock,
    header: &HeaderView,
    consensus: &Consensus,
    active_chain: &ActiveChain,
) -> Status {
    if compact_block.uncles().len() > consensus.max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock uncles count({}) > consensus max_uncles_num({})",
            compact_block.uncles().len(),
            consensus.max_uncles_num()
        ));
    }
    if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock proposals count({}) > consensus max_block_proposals_limit({})",
            compact_block.proposals().len(),
            consensus.max_block_proposals_limit(),
        ));
    }

    // Only accept blocks with a height greater than tip - N
    // where N is the current epoch length
    let block_hash = header.hash();
    let tip = active_chain.tip_header();
    let epoch_length = active_chain.epoch_ext().length();
    let lowest_number = tip.number().saturating_sub(epoch_length);

    if lowest_number > header.number() {
        return StatusCode::CompactBlockIsStaled.with_context(block_hash);
    }

    Status::ok()
}
```

**File:** sync/src/relayer/compact_block_process.rs (L230-268)
```rust
async fn contextual_check(
    compact_block_header: &HeaderView,
    shared: &Arc<SyncShared>,
    active_chain: &ActiveChain,
    nc: &Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
) -> Status {
    let block_hash = compact_block_header.hash();
    let tip = active_chain.tip_header();

    let status = active_chain.get_block_status(&block_hash);
    if status.contains(BlockStatus::BLOCK_STORED) {
        // update last common header and best known
        let parent = shared
            .get_header_index_view(&compact_block_header.data().raw().parent_hash(), true)
            .expect("parent block must exist");

        let header_index = HeaderIndex::new(
            compact_block_header.number(),
            block_hash.clone(),
            parent.total_difficulty() + compact_block_header.difficulty(),
        );
        let state = shared.state().peers();
        state.may_set_best_known_header(peer, header_index);

        return StatusCode::CompactBlockAlreadyStored.with_context(block_hash);
    } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
        // block already in orphan pool
        return Status::ignored();
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }

    let store_first = tip.number() + 1 >= compact_block_header.number();
    let parent = shared.get_header_index_view(
        &compact_block_header.data().raw().parent_hash(),
        store_first,
    );
    if parent.is_none() {
```
