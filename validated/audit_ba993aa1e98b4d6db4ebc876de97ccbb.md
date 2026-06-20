### Title
Unbounded Loop with Repeated Database Lookups in `locate_latest_common_block` Triggered by Peer `GetHeaders` Message — (`File: sync/src/types/mod.rs`)

### Summary

`ActiveChain::locate_latest_common_block` contains an unbounded `loop` that performs two RocksDB lookups per iteration while walking a fork's parent-hash chain. A peer that has previously relayed valid orphaned blocks can send a crafted `GetHeaders` message whose locator points into a long fork chain stored on the victim node, causing the loop to run for an arbitrarily large number of iterations. Because the handler is invoked under `tokio::task::block_in_place`, the loop executes synchronously on a tokio worker thread, blocking the sync subsystem for the duration.

### Finding Description

**Root cause — `sync/src/types/mod.rs`, lines 1883–1899:**

```rust
if let Some(header) = locator
    .get(index - 1)
    .and_then(|hash| self.sync_shared.store().get_block_header(hash))  // DB lookup #1
{
    let mut block_hash = header.data().raw().parent_hash();
    loop {                                                               // ← unbounded
        let block_header = match self.sync_shared.store()
            .get_block_header(&block_hash) {                            // DB lookup #2 per iter
            None => break latest_common,
            Some(block_header) => block_header,
        };
        if let Some(block_number) = self.snapshot
            .get_block_number(&block_hash) {                            // DB lookup #3 per iter
            return Some(block_number);
        }
        block_hash = block_header.data().raw().parent_hash();           // follow parent
    }
}
```

The loop terminates only when either `get_block_header` returns `None` (block absent from store) or `get_block_number` returns `Some` (block is on the main chain). There is no iteration counter or depth cap. Each iteration issues two synchronous RocksDB reads. [1](#0-0) 

**Entry path:**

A remote peer sends a `SyncMessage::GetHeaders` P2P message. The handler dispatches to `GetHeadersProcess::execute()`, which calls `active_chain.locate_latest_common_block()`. [2](#0-1) 

The handler is wrapped in `tokio::task::block_in_place`, making the entire call synchronous on a tokio worker thread: [3](#0-2) 

**Locator size is bounded, but the inner loop is not:**

`GetHeadersProcess::execute()` rejects locators larger than `MAX_LOCATOR_SIZE = 101`. This limits the outer scan over the locator array, but the inner `loop` that walks the parent-hash chain of a fork block is completely unbounded. [4](#0-3) 

**Trigger condition:**

The inner loop executes when `locator[index-1]` is a hash that (a) exists in the node's block store and (b) is NOT on the current main chain. The loop then walks every ancestor of that fork block until it reaches a main-chain block or exhausts the store. If the fork chain stored on the victim node is N blocks deep, the loop performs O(N) RocksDB reads. [5](#0-4) 

### Impact Explanation

- **Sync thread starvation:** `block_in_place` blocks a tokio worker thread for the duration of the loop. A long fork chain (e.g., thousands of orphaned blocks) causes the sync subsystem to stall, preventing the node from processing other sync messages, fetching blocks, or responding to peers.
- **Repeated triggering:** The attacker can send `GetHeaders` messages continuously. Each message re-enters the loop with no rate-limit specific to this code path beyond general peer message handling.
- **Cascading effect:** Stalling the sync thread delays block propagation and can cause the victim node to fall behind the chain tip, degrading its ability to participate in consensus.

### Likelihood Explanation

The attacker must have previously caused the victim node to store a long fork chain. This is achievable by:

1. **Natural fork exploitation:** Any node that has experienced a natural fork (common during IBD or network partitions) already has orphaned blocks in its store. An attacker who knows the fork hashes (observable from the network) can immediately exploit this.
2. **Deliberate fork injection:** A malicious block relayer submits a sequence of valid PoW blocks on a side chain. Once stored, the attacker repeatedly sends `GetHeaders` messages pointing into that chain. The cost is proportional to the fork length, but the attack can be replayed indefinitely at zero additional cost after setup.

The `GetHeaders` message is accepted from any connected peer without authentication.

### Recommendation

Add an explicit iteration cap inside the `loop` in `locate_latest_common_block`. A limit of, for example, `MAX_LOCATOR_SIZE` (101) iterations is sufficient for the protocol's purpose, since the locator already encodes a logarithmically-spaced set of checkpoints:

```rust
let mut depth = 0usize;
let max_depth = MAX_LOCATOR_SIZE; // or a dedicated constant
loop {
    if depth >= max_depth {
        break latest_common;
    }
    depth += 1;
    // ... existing body ...
}
```

Alternatively, restructure the search to use the skip-list (`HeaderIndexView::get_ancestor`) which is already O(log N) and bounded.

### Proof of Concept

1. Attacker connects to a victim CKB node as a peer.
2. Attacker relays `N` valid PoW blocks forming a side chain branching from block 0 (genesis). The victim stores all `N` blocks but keeps the main chain.
3. Attacker sends a `SyncMessage::GetHeaders` with `block_locator_hashes = [side_chain_tip_hash, genesis_hash]`.
4. In `locate_latest_common_block`:
   - `locator.last()` == genesis hash ✓
   - The lazy iterator finds `genesis_hash` at `index = 1` (it is on the main chain at block 0).
   - `locator[index-1]` = `side_chain_tip_hash` → `get_block_header` succeeds (block is stored).
   - The inner `loop` walks all `N` parent hashes, issuing 2 RocksDB reads per step, until it reaches genesis.
5. With `N = 10,000` orphaned blocks, the loop performs ~20,000 synchronous DB reads while holding a tokio worker thread, stalling the sync subsystem.
6. The attacker repeats step 3 continuously to sustain the denial of service. [6](#0-5) [7](#0-6)

### Citations

**File:** sync/src/types/mod.rs (L1857-1903)
```rust
    pub fn locate_latest_common_block(
        &self,
        _hash_stop: &Byte32,
        locator: &[Byte32],
    ) -> Option<BlockNumber> {
        if locator.is_empty() {
            return None;
        }

        let locator_hash = locator.last().expect("empty checked");
        if locator_hash != &self.sync_shared.consensus().genesis_hash() {
            return None;
        }

        // iterator are lazy
        let (index, latest_common) = locator
            .iter()
            .enumerate()
            .map(|(index, hash)| (index, self.snapshot.get_block_number(hash)))
            .find(|(_index, number)| number.is_some())
            .expect("locator last checked");

        if index == 0 || latest_common == Some(0) {
            return latest_common;
        }

        if let Some(header) = locator
            .get(index - 1)
            .and_then(|hash| self.sync_shared.store().get_block_header(hash))
        {
            let mut block_hash = header.data().raw().parent_hash();
            loop {
                let block_header = match self.sync_shared.store().get_block_header(&block_hash) {
                    None => break latest_common,
                    Some(block_header) => block_header,
                };

                if let Some(block_number) = self.snapshot.get_block_number(&block_hash) {
                    return Some(block_number);
                }

                block_hash = block_header.data().raw().parent_hash();
            }
        } else {
            latest_common
        }
    }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L36-69)
```rust
    pub fn execute(self) -> Status {
        let active_chain = self.synchronizer.shared.active_chain();

        let block_locator_hashes = self
            .message
            .block_locator_hashes()
            .iter()
            .map(|x| x.to_entity())
            .collect::<Vec<Byte32>>();
        let hash_stop = self.message.hash_stop().to_entity();
        let locator_size = block_locator_hashes.len();
        if locator_size > MAX_LOCATOR_SIZE {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "Locator count({locator_size}) > MAX_LOCATOR_SIZE({MAX_LOCATOR_SIZE})"
            ));
        }

        if active_chain.is_initial_block_download() {
            info!(
                "Ignoring getheaders from peer={} because the node is in initial block download stage.",
                self.peer
            );
            self.send_in_ibd();
            let shared = self.synchronizer.shared();
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
            return Status::ignored();
        }

        if let Some(block_number) =
            active_chain.locate_latest_common_block(&hash_stop, &block_locator_hashes[..])
```

**File:** sync/src/synchronizer/mod.rs (L397-401)
```rust
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** util/constant/src/sync.rs (L44-45)
```rust
/// The maximum number of entries in a locator
pub const MAX_LOCATOR_SIZE: usize = 101;
```
