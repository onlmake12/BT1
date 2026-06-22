### Title
Light Client Block Filter Protocol Serves Reorged Data Without Hash Anchoring — (`sync/src/filter/get_block_filters_process.rs`, `sync/src/filter/get_block_filter_hashes_process.rs`, `sync/src/filter/get_block_filter_check_points_process.rs`)

---

### Summary

The three block-filter P2P protocol handlers (`GetBlockFilters`, `GetBlockFilterHashes`, `GetBlockFilterCheckPoints`) accept a bare `start_number` (block number) from any peer and serve filter data keyed only on that number. No block hash is included in the request to anchor the response to a specific chain state. During a chain reorganization the blocks at those numbers silently change, so the server returns filter data that belongs to a different fork than the one the client was tracking. `GetBlockFilterCheckPoints` and `GetBlockFilterHashes` do not even include block hashes in their responses, so the receiving light client has no in-band signal that the data has shifted to a different chain.

---

### Finding Description

**Root cause — number-only lookup, no hash anchor**

All three handlers extract `start_number` from the incoming message and immediately call `active_chain.get_block_hash(block_number)` in a loop:

```
// get_block_filters_process.rs
let start_number: BlockNumber = self.message.to_entity().start_number().into();
...
if let Some(block_hash) = active_chain.get_block_hash(block_number) {
``` [1](#0-0) 

The same pattern appears in the hashes handler:

```
let start_number: BlockNumber = self.message.to_entity().start_number().into();
...
active_chain.get_block_hash(start_number - 1)
``` [2](#0-1) 

And in the checkpoints handler:

```
let start_number: BlockNumber = self.message.to_entity().start_number().into();
...
active_chain.get_block_hash(block_number)
    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
``` [3](#0-2) 

The wire schema confirms that the request messages carry only a number, never a hash:

```
struct GetBlockFilters        { start_number: Uint64, }
struct GetBlockFilterHashes   { start_number: Uint64, }
struct GetBlockFilterCheckPoints { start_number: Uint64, }
``` [4](#0-3) 

**Missing hash in responses**

`BlockFilterCheckPoints` and `BlockFilterHashes` return only filter hashes, not the block hashes that produced them:

```
table BlockFilterHashes {
    start_number:               Uint64,
    parent_block_filter_hash:   Byte32,
    block_filter_hashes:        Byte32Vec,   // ← no block_hashes field
}
table BlockFilterCheckPoints {
    start_number:           Uint64,
    block_filter_hashes:    Byte32Vec,       // ← no block_hashes field
}
``` [5](#0-4) 

`BlockFilters` does include `block_hashes`, but the client still cannot detect that the chain shifted because the request carried no expected-hash anchor.

**Contrast with the light-client proof protocol**

The `GetLastStateProof` message — which is security-critical — correctly includes both `start_hash` and `start_number` so the server can verify the client's known chain state before responding:

```
table GetLastStateProof {
    last_hash:    Byte32,
    start_hash:   Byte32,
    start_number: Uint64,
    ...
}
``` [6](#0-5) 

The block-filter protocol has no equivalent protection.

**Reorg scenario**

1. Light client observes block `H` at number `N` and decides to request filters starting at `N`.
2. Before the request arrives, a reorg replaces block `H` with block `H'` at number `N`.
3. The server calls `get_block_hash(N)` and returns `H'`'s hash, then serves filters built from `H'` and its successors.
4. For `GetBlockFilterCheckPoints` / `GetBlockFilterHashes`, the response contains no block hashes at all, so the client cannot detect the substitution.
5. The client updates its local filter state as if it had scanned the original chain, silently missing or misattributing transactions.

The `build_filter_data` function in the block-filter builder already handles reorgs on the server side by walking back to the fork point, but the P2P serving layer has no equivalent guard: [7](#0-6) 

---

### Impact Explanation

A light client that relies on `GetBlockFilterCheckPoints` or `GetBlockFilterHashes` to verify its sync state can be silently fed filter data from a reorganized (minority) fork. Because neither response includes block hashes, the client has no in-band way to detect the mismatch. Concretely:

- **Missed incoming payments**: A transaction confirmed in the canonical chain may not appear in the filters the client received (they cover a different fork), so the client never downloads the confirming block and never credits the payment.
- **False confirmation**: A transaction that was confirmed on the fork but later reorganized away may appear in the filters the client already accepted, causing the client to believe the payment is final when it has been reversed.

Both outcomes can result in direct financial loss for users of wallets or services built on the CKB light-client filter protocol.

---

### Likelihood Explanation

- **Entry path**: Any peer that speaks the `Filter` protocol can send `GetBlockFilters`, `GetBlockFilterHashes`, or `GetBlockFilterCheckPoints` with an arbitrary `start_number`. No authentication or privilege is required.
- **Reorg trigger**: CKB reorgs occur naturally during normal network operation (competing miners, network partitions). An adversary who can influence block propagation timing can increase the probability of a reorg coinciding with a filter request.
- **Window**: The race window is the round-trip time of the filter request, which is on the order of seconds — long enough for a shallow reorg to occur.

---

### Recommendation

1. **Add a `start_hash` field to all three request messages** alongside `start_number`, mirroring the `GetLastStateProof` design. The server should verify `get_block_hash(start_number) == start_hash` before serving any data; if the hashes differ, it should return an error or a reorg-notification response.

2. **Add `block_hashes` to `BlockFilterHashes` and `BlockFilterCheckPoints` responses** so clients can independently verify which blocks the filter hashes correspond to, even if the request-side anchor is not added.

---

### Proof of Concept

```
// Attacker or natural reorg scenario:
// 1. Full node is at tip T (number N).
// 2. Light client sends: GetBlockFilters { start_number: N-5 }
// 3. Reorg occurs: blocks N-5 .. N are replaced by N-5' .. N'.
// 4. Server's active_chain.get_block_hash(N-5) now returns hash(N-5').
// 5. Server responds with filters for N-5' .. N', echoing start_number = N-5.
// 6. Light client stores these filters, believing they cover the original chain.
// 7. Any transaction in blocks N-5 .. N (original) is now invisible to the client.
// 8. For GetBlockFilterCheckPoints/Hashes, the response has no block_hashes field,
//    so the client cannot detect the substitution at all.
```

The handler code that performs the unchecked number-to-hash lookup is at: [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** sync/src/filter/get_block_filters_process.rs (L33-85)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        if latest >= start_number {
            let mut block_hashes = Vec::new();
            let mut filters = Vec::new();
            let mut current_content_size = 0;
            current_content_size += 8; // Size of start_number
            current_content_size += 4 * 2; // Size of the header field `full-size` of `block_hash` and `block_filter`
            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_hash) = active_chain.get_block_hash(block_number) {
                    if let Some(block_filter) = active_chain.get_block_filter(&block_hash) {
                        if current_content_size
                            + block_hash.as_slice().len()
                            + 4
                            + block_filter.as_slice().len()
                            + 4
                            >= (1.8 * 1024.0 * 1024.0) as usize
                        {
                            // Break if the encoded size of `block_hash` + `block_filter` + `start_number` + molecule header increase reaches 1.8MB, to avoid frame size too large
                            break;
                        }
                        current_content_size +=
                            block_hash.as_slice().len() + block_filter.as_slice().len() + 4;
                        block_hashes.push(block_hash);
                        filters.push(block_filter);
                    } else {
                        break;
                    }
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilters::new_builder()
                .start_number(start_number)
                .block_hashes(block_hashes)
                .filters(filters)
                .build();
            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L32-80)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
            let parent_block_filter_hash = if start_number > 0 {
                match active_chain
                    .get_block_hash(start_number - 1)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    Some(parent_block_filter_hash) => parent_block_filter_hash,
                    None => return Status::ignored(),
                }
            } else {
                packed::Byte32::zero()
            };

            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(1) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilterHashes::new_builder()
                .start_number(start_number)
                .parent_block_filter_hash(parent_block_filter_hash)
                .block_filter_hashes(block_filter_hashes)
                .build();

            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-69)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(CHECK_POINT_INTERVAL) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilterCheckPoints::new_builder()
                .start_number(start_number)
                .block_filter_hashes(block_filter_hashes)
                .build();

            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** util/gen-types/schemas/extensions.mol (L211-238)
```text
struct GetBlockFilters {
    start_number:   Uint64,
}

table BlockFilters {
    start_number:   Uint64,
    block_hashes:   Byte32Vec,
    filters:        BytesVec,
}

struct GetBlockFilterHashes {
    start_number:   Uint64,
}

table BlockFilterHashes {
    start_number:               Uint64,
    parent_block_filter_hash:   Byte32,
    block_filter_hashes:        Byte32Vec,
}

struct GetBlockFilterCheckPoints {
    start_number:   Uint64,
}

table BlockFilterCheckPoints {
    start_number:           Uint64,
    block_filter_hashes:    Byte32Vec,
}
```

**File:** util/gen-types/schemas/extensions.mol (L324-342)
```text
table GetLastStateProof {
    // The last block hash known by the client.
    // It could be different with the tip hash in the server.
    last_hash:                  Byte32,

    // The hash of the last proved block.
    start_hash:                 Byte32,
    // The block number of the last proved block.
    start_number:               Uint64,

    // How many continuous blocks before the tip block should be included at
    // least, if possible?
    last_n_blocks:              Uint64,
    // All blocks, whose total difficulty is not less than this difficulty
    // boundary, should be included in the proof.
    difficulty_boundary:        Uint256,
    // The sampled difficulties.
    difficulties:               Uint256Vec,
}
```

**File:** block-filter/src/filter.rs (L78-108)
```rust
        let start_number = match snapshot.get_latest_built_filter_data_block_hash() {
            Some(block_hash) => {
                debug!("Hash of the latest created block {:#x}", block_hash);
                if snapshot.is_main_chain(&block_hash) {
                    let header = snapshot
                        .get_block_header(&block_hash)
                        .expect("header stored");
                    debug!(
                        "Latest created block on the main chain, starting from {}",
                        header.number() + 1
                    );
                    header.number() + 1
                } else {
                    // find fork chain number
                    let mut header = snapshot
                        .get_block_header(&block_hash)
                        .expect("header stored");
                    while !snapshot.is_main_chain(&header.parent_hash()) {
                        header = snapshot
                            .get_block_header(&header.parent_hash())
                            .expect("parent header stored");
                    }
                    debug!(
                        "Block with the latest built filter data on the forked chain, starting from {}",
                        header.number()
                    );
                    header.number()
                }
            }
            None => 0,
        };
```
