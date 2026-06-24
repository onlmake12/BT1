Audit Report

## Title
Unbounded `peers_map` with Per-Peer `reconstruct_block` Under Held Mutex Enables Relay Amplification DoS — (`sync/src/relayer/block_transactions_process.rs`)

## Summary
`PendingCompactBlockMap`'s inner `HashMap<PeerIndex, …>` has no size cap, allowing M attacker-controlled peers to each register for the same compact block hash. When each peer sends a partial `BlockTransactions` response, `BlockTransactionsProcess::execute` independently calls `reconstruct_block` (including `tx_pool.fetch_txs`) for each peer while holding the global `pending_compact_blocks` `tokio::sync::Mutex` across the await point. This serializes M sequential reconstruction attempts behind a single mutex, blocking all compact block relay processing on the node for O(M × fetch_txs_latency) per round, indefinitely repeatable.

## Finding Description

**Root cause 1 — Unbounded `peers_map`:**

`PendingCompactBlockMap` is defined with no size limit on the inner peer map:

```rust
pub(crate) type PendingCompactBlockMap = HashMap<
    Byte32,
    (packed::CompactBlock, HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>, u64),
>;
``` [1](#0-0) 

`missing_or_collided_post_process` inserts each new peer unconditionally with no cap:

```rust
.insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
``` [2](#0-1) 

**Root cause 2 — `contextual_check` only deduplicates the same peer, not total peer count:**

```rust
if pending_compact_blocks
    .get(&block_hash)
    .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
    .unwrap_or(false)
{ return StatusCode::CompactBlockIsAlreadyPending… }
``` [3](#0-2) 

This allows M distinct peers to each register for the same block hash.

**Root cause 3 — `tokio::sync::Mutex` guard held across `reconstruct_block` await:**

The guard is acquired at line 68 (`.await`) and the `Entry::Occupied` borrow keeps it alive for the entire `if let` block through line 187, spanning both the `reconstruct_block` await and the `async_send_message_to` await:

```rust
if let Entry::Occupied(mut pending) = shared
    .state()
    .pending_compact_blocks()
    .await                          // MutexGuard acquired
    .entry(block_hash.clone())
{
    …
    let ret = self.relayer.reconstruct_block(…).await;  // guard still held
    …
    let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;  // guard still held
}  // MutexGuard dropped here
``` [4](#0-3) 

`reconstruct_block` calls `tx_pool.fetch_txs` for every missing short ID:

```rust
if !short_ids_set.is_empty() {
    let tx_pool = self.shared.shared().tx_pool_controller();
    let fetch_txs = tx_pool.fetch_txs(short_ids_set).await;
``` [5](#0-4) 

There is no "reconstruction already in progress" flag or deduplication across peers for the same block hash. Each of M peers' `BlockTransactions` messages independently acquires the mutex and awaits `reconstruct_block`.

**Exploit flow:**
1. Attacker connects M peers (P1…PM) to victim.
2. Each Pi sends `CompactBlock(H)` with short_ids=[tx_A, tx_B] not in victim's tx pool → each Pi is inserted into `peers_map[H]`.
3. Each Pi replies with `BlockTransactions(H, [tx_A])` (deliberately omitting tx_B).
4. Victim processes P1's message: acquires mutex → `reconstruct_block` → `fetch_txs({tx_B})` → still missing → sends `GetBlockTransactions([1])` to P1 → releases mutex.
5. Victim processes P2's message: same sequence, mutex held again across another `fetch_txs` call.
6. Repeated for P3…PM. Each Pi receives `GetBlockTransactions` and replies with `tx_A` again → cycle repeats indefinitely.

## Impact Explanation
The `pending_compact_blocks` mutex is held for O(M × fetch_txs_latency) per round, serializing all compact block relay processing behind M sequential async I/O operations. This constitutes a **High** impact: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." The victim node's relay pipeline stalls, preventing timely compact block reconstruction and propagation, degrading the node's ability to follow the chain tip.

## Likelihood Explanation
The attack requires only M standard inbound P2P connections sending valid (but incomplete) compact block relay messages — no PoW, no keys, no privileged access. The attacker's cost per round is O(M) small messages. The victim's cost is O(M) `tx_pool.fetch_txs` calls and O(M × fetch_txs_latency) of mutex hold time. The cycle is self-sustaining as long as the attacker withholds one transaction. This is concretely reachable on mainnet within the node's `max_inbound` peer limit.

## Recommendation
1. **Cap `peers_map` per block hash** — in `missing_or_collided_post_process`, reject insertion if `peers_map.len() >= MAX_COMPACT_BLOCK_PEERS` (e.g., 3–5).
2. **Release the mutex before awaiting** — in `execute`, clone the needed data out of `pending_compact_blocks`, drop the `MutexGuard`, then call `reconstruct_block`. Re-acquire the mutex only to update state afterward.
3. **Deduplicate reconstruction work** — once one peer has triggered `reconstruct_block` for a given block hash with a `Missing` result, record a flag; subsequent peers' `BlockTransactions` for the same hash should only update their own `peers_map` entry and re-send `GetBlockTransactions` without calling `reconstruct_block` again.

## Proof of Concept
```
1. Attacker connects M peers (P1…PM) to victim node.
2. Each Pi sends CompactBlock(H) with short_ids=[tx_A, tx_B], prefilled=[],
   where tx_A and tx_B are absent from victim's tx pool.
   → contextual_check passes (each Pi is a distinct PeerIndex)
   → missing_or_collided_post_process inserts Pi into peers_map[H]
   → GetBlockTransactions([0,1]) sent to Pi

3. Each Pi replies with BlockTransactions(H, transactions=[tx_A])
   (deliberately omitting tx_B — still one tx missing).

4. For each Pi (sequentially, due to mutex):
   - Victim acquires pending_compact_blocks mutex
   - Calls reconstruct_block → tx_pool.fetch_txs({tx_B}) → Missing
   - Sends GetBlockTransactions([1]) to Pi
   - Releases mutex

5. Each Pi receives GetBlockTransactions and replies with tx_A again → goto 4.

Assert: M tx_pool.fetch_txs calls per round, mutex held for
O(M × fetch_txs_latency), blocking all other compact block relay.
```

### Citations

**File:** sync/src/types/mod.rs (L979-987)
```rust
// <CompactBlockHash, (CompactBlock, <PeerIndex, (Vec<TransactionsIndex>, Vec<UnclesIndex>)>, timestamp)>
pub(crate) type PendingCompactBlockMap = HashMap<
    Byte32,
    (
        packed::CompactBlock,
        HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>,
        u64,
    ),
>;
```

**File:** sync/src/relayer/compact_block_process.rs (L284-291)
```rust
    let pending_compact_blocks = shared.state().pending_compact_blocks().await;
    if pending_compact_blocks
        .get(&block_hash)
        .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
        .unwrap_or(false)
    {
        return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L354-361)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
```

**File:** sync/src/relayer/block_transactions_process.rs (L65-187)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
            let (compact_block, peers_map, _) = pending.get_mut();
            if let Entry::Occupied(mut value) = peers_map.entry(self.peer) {
                let (expected_transaction_indexes, expected_uncle_indexes) = value.get_mut();
                ckb_logger::info!(
                    "relayer receive BLOCKTXN of {}, peer: {}",
                    block_hash,
                    self.peer
                );

                attempt!(BlockTransactionsVerifier::verify(
                    compact_block,
                    expected_transaction_indexes,
                    &received_transactions,
                ));
                attempt!(BlockUnclesVerifier::verify(
                    compact_block,
                    expected_uncle_indexes,
                    &received_uncles,
                ));

                let ret = self
                    .relayer
                    .reconstruct_block(
                        &active_chain,
                        compact_block,
                        received_transactions,
                        expected_uncle_indexes,
                        &received_uncles,
                    )
                    .await;

                // Request proposal
                {
                    let proposals: Vec<_> = received_uncles
                        .into_iter()
                        .flat_map(|u| u.data().proposals().into_iter())
                        .collect();
                    self.relayer.request_proposal_txs(
                        &self.nc,
                        self.peer,
                        (
                            compact_block.header().into_view().number(),
                            block_hash.clone(),
                        )
                            .into(),
                        proposals,
                    );
                }

                match ret {
                    ReconstructionResult::Block(block) => {
                        pending.remove();
                        self.relayer
                            .accept_block(self.nc, self.peer, block, "BlockTransactions");
                        return Status::ok();
                    }
                    ReconstructionResult::Missing(transactions, uncles) => {
                        // We need to get all transactions and uncles that do not exist locally
                        // at once to restore a block
                        //
                        // Under normal circumstances, one request is enough, when the chain occurs fork,
                        // the transaction pool may drop some transactions due to double spend check, at
                        // this time, the previously issued request to obtain transactions may not meet
                        // the needs of a one-time construction, we need to send another complete request
                        // to do so. That is, the current miss + the miss of the previous request are
                        // combined and requested once. This is a small probability event
                        missing_transactions = transactions
                            .into_iter()
                            .map(|i| i as u32)
                            .chain(expected_transaction_indexes.iter().copied())
                            .collect();
                        missing_uncles = uncles
                            .into_iter()
                            .map(|i| i as u32)
                            .chain(expected_uncle_indexes.iter().copied())
                            .collect();

                        missing_transactions.sort_unstable();
                        missing_uncles.sort_unstable();
                    }
                    ReconstructionResult::Collided => {
                        missing_transactions = compact_block
                            .short_id_indexes()
                            .into_iter()
                            .map(|i| i as u32)
                            .collect();
                        collision = true;
                        missing_uncles = vec![];
                    }
                    ReconstructionResult::Error(status) => {
                        return status;
                    }
                }

                assert!(!missing_transactions.is_empty() || !missing_uncles.is_empty());

                let content = packed::GetBlockTransactions::new_builder()
                    .block_hash(block_hash.clone())
                    .indexes(missing_transactions.as_slice())
                    .uncle_indexes(missing_uncles.as_slice())
                    .build();
                let message = packed::RelayMessage::new_builder().set(content).build();

                let _ignore = async_send_message_to(&self.nc, self.peer, &message).await;

                let _ignore_prev_value =
                    mem::replace(expected_transaction_indexes, missing_transactions);
                let _ignore_prev_value = mem::replace(expected_uncle_indexes, missing_uncles);

                if collision {
                    return StatusCode::CompactBlockMeetsShortIdsCollision.with_context(block_hash);
                } else {
                    return StatusCode::CompactBlockRequiresFreshTransactions
                        .with_context(block_hash);
                }
            }
        }
```

**File:** sync/src/relayer/mod.rs (L386-393)
```rust
        if !short_ids_set.is_empty() {
            let tx_pool = self.shared.shared().tx_pool_controller();
            let fetch_txs = tx_pool.fetch_txs(short_ids_set).await;
            if let Err(e) = fetch_txs {
                return ReconstructionResult::Error(StatusCode::TxPool.with_context(e));
            }
            txs_map.extend(fetch_txs.unwrap());
        }
```
