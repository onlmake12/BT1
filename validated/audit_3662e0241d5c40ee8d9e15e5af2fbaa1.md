Audit Report

## Title
Unbounded O(fork_depth) DB Read Loop in `locate_latest_common_block` Triggered by Stored Fork Headers — (`sync/src/types/mod.rs`)

## Summary

The inner loop of `locate_latest_common_block` walks `parent_hash` links through the store with no depth bound, performing two DB reads per iteration. An attacker who has stored N fork-chain blocks can craft a `GetHeaders` locator where `locator[index-1]` is the fork tip, triggering O(N) RocksDB reads per message. Because a successful call returns `Status::ok()` (no ban), the attacker can repeat this indefinitely with O(1) per-message effort.

## Finding Description

`locate_latest_common_block` first scans the locator for the first hash present in the snapshot (canonical chain): [1](#0-0) 

When `index > 0` and `latest_common != Some(0)`, it fetches `locator[index-1]` from the **store** (not the snapshot — fork/detached blocks qualify): [2](#0-1) 

It then enters an unbounded loop walking `parent_hash` links, performing one `store().get_block_header()` and one `snapshot.get_block_number()` per iteration, with no depth cap: [3](#0-2) 

`MAX_LOCATOR_SIZE = 101` only bounds the locator array length, not the depth of this inner traversal: [4](#0-3) 

When the function returns `Some(block_number)` (the attack succeeds), the handler returns `Status::ok()` — the peer is **not banned**: [5](#0-4) 

The ban (`SYNC_USELESS_BAN_TIME`) only fires when the function returns `None` (`GetHeadersMissCommonAncestors`): [6](#0-5) 

`GetHeadersProcess::execute` runs inside `tokio::task::block_in_place`, blocking the sync thread pool for the full duration of the loop: [7](#0-6) 

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** Once the one-time fork-chain setup is complete, each subsequent `GetHeaders` message costs the attacker O(1) effort while costing the victim O(N) synchronous RocksDB reads inside `block_in_place`. Blocking the sync thread pool degrades header/block sync for all connected peers, causing network-level congestion and sync stalls on the targeted node.

## Likelihood Explanation

**Setup cost**: Storing N fork blocks requires mining N valid PoW blocks. On mainnet this is expensive; on testnet or low-difficulty devnets it is cheap. The setup is a one-time cost — once stored, the fork chain persists in the node's RocksDB. **Ongoing cost**: Each exploit is a single `GetHeaders` P2P packet. No rate limiting, no ban, and no connection teardown is triggered by a successful (but expensive) `locate_latest_common_block` call. The attacker-to-victim work amplification ratio grows linearly with fork depth N.

## Recommendation

Add a depth/iteration cap to the inner loop in `locate_latest_common_block`: [3](#0-2) 

Introduce a counter (e.g., `max_steps = 64`) and `break` returning `latest_common` once the limit is exceeded. The legitimate use case — refining the common ancestor between two adjacent locator entries — does not require walking an arbitrarily deep fork chain.

## Proof of Concept

1. Start a CKB node on a low-difficulty devnet.
2. Mine N fork blocks branching from any canonical block (using `Switch::DISABLE_EXTENSION` as done in existing tests to bypass full verification overhead).
3. Submit all N fork blocks to the target node via `SendBlock` messages; confirm `BlockStatus::BLOCK_STORED` for each.
4. Craft a `GetHeaders` locator where:
   - `locator[0]` = fork tip hash (in store, not in snapshot)
   - `locator[1]` = genesis hash (in snapshot, satisfies the genesis check at line 1867)
5. Send the message repeatedly. Instrument `sync_shared.store().get_block_header` call count per `execute()` invocation and assert it equals N.
6. Observe that the sync thread is blocked for O(N) DB reads per message with no ban or rate limiting applied, and that connected peers experience sync stalls.

### Citations

**File:** sync/src/types/mod.rs (L1872-1877)
```rust
        let (index, latest_common) = locator
            .iter()
            .enumerate()
            .map(|(index, hash)| (index, self.snapshot.get_block_number(hash)))
            .find(|(_index, number)| number.is_some())
            .expect("locator last checked");
```

**File:** sync/src/types/mod.rs (L1883-1886)
```rust
        if let Some(header) = locator
            .get(index - 1)
            .and_then(|hash| self.sync_shared.store().get_block_header(hash))
        {
```

**File:** sync/src/types/mod.rs (L1887-1899)
```rust
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
```

**File:** util/constant/src/sync.rs (L44-45)
```rust
/// The maximum number of entries in a locator
pub const MAX_LOCATOR_SIZE: usize = 101;
```

**File:** sync/src/synchronizer/get_headers_process.rs (L68-98)
```rust
        if let Some(block_number) =
            active_chain.locate_latest_common_block(&hash_stop, &block_locator_hashes[..])
        {
            debug!(
                "headers latest_common={} tip={} begin",
                block_number,
                active_chain.tip_header().number(),
            );

            self.synchronizer.peers().getheaders_received(self.peer);
            let headers: Vec<core::HeaderView> =
                active_chain.get_locator_response(block_number, &hash_stop);
            // response headers

            debug!("headers len={}", headers.len());

            let content = packed::SendHeaders::new_builder()
                .headers(headers.into_iter().map(|x| x.data()).collect::<Vec<_>>())
                .build();
            let message = packed::SyncMessage::new_builder().set(content).build();
            let nc = Arc::clone(self.nc);
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
        } else {
            return StatusCode::GetHeadersMissCommonAncestors
                .with_context(format!("{block_locator_hashes:#x?}"));
        }
        Status::ok()
```

**File:** sync/src/status.rs (L176-179)
```rust
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```

**File:** sync/src/synchronizer/mod.rs (L397-401)
```rust
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
```
