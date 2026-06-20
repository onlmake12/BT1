Let me look at the tx-pool service loop and the `compare_with_pending_compact` call sites to complete the analysis.

### Title
Tokio Mutex `pending_compact_blocks` Held Across Async `reconstruct_block` / `tx_pool.fetch_txs` — (`sync/src/relayer/block_transactions_process.rs`)

---

### Summary

`BlockTransactionsProcess::execute` acquires the global `pending_compact_blocks` `tokio::sync::Mutex` and holds the guard across an `.await` on `reconstruct_block`, which in turn calls `tx_pool.fetch_txs(...).await` — a channel round-trip to the tx-pool service. Any concurrent task that needs the same lock (`CompactBlockProcess::execute`, `missing_or_collided_post_process`, `compare_with_pending_compact`) is blocked for the entire duration of that round-trip. Because the tx-pool service processes messages sequentially, an attacker who pre-fills the service channel with `SubmitRemoteTx` messages can extend that duration arbitrarily, stalling compact-block relay for every peer simultaneously.

---

### Finding Description

**Lock acquisition and guard lifetime**

`pending_compact_blocks()` returns a `tokio::sync::MutexGuard<'_, PendingCompactBlockMap>`. The `Entry::Occupied(mut pending)` value borrows from that guard, so the guard is kept alive for the entire `if let` block — including the `.await` on `reconstruct_block`: [1](#0-0) 

**`reconstruct_block` awaits the tx-pool service**

Inside `reconstruct_block`, when any short IDs remain unresolved from the received transactions, the function sends a `FetchTxs` message to the tx-pool service channel and `await`s the one-shot response: [2](#0-1) 

**`fetch_txs` is a full channel round-trip**

`fetch_txs` sends to `self.sender` (a bounded async channel) and then `await`s the `oneshot` response. Both awaits happen while the `pending_compact_blocks` guard is live: [3](#0-2) 

**The tx-pool service loop is sequential**

The service loop dispatches one message at a time. `SubmitRemoteTx` calls `service.submit_remote_tx(...).await`, which can involve script verification scheduling. Every message queued ahead of `FetchTxs` must complete before the response is sent: [4](#0-3) 

**All other compact-block paths contend on the same lock**

`CompactBlockProcess::execute` calls `pending_compact_blocks().await` twice (lines 106 and 284). `missing_or_collided_post_process` calls it again (line 356). All block on the same `tokio::sync::Mutex`: [5](#0-4) [6](#0-5) 

**`compare_with_pending_compact` uses `blocking_lock`**

The sync-layer block fetcher calls `compare_with_pending_compact`, which uses `blocking_lock()` on the same mutex. If the async guard is held by a stalled `BlockTransactionsProcess` task, this call parks the calling thread: [7](#0-6) 

**`PendingCompactBlockMap` type and mutex declaration** [8](#0-7) [9](#0-8) 

---

### Impact Explanation

While the `pending_compact_blocks` lock is held, no other peer's compact block can be inserted, looked up, or removed. A single slow `BlockTransactions` message from any peer stalls the entire compact-block relay subsystem for all peers. Block propagation latency increases, the node falls behind the chain tip, and uncle rates rise. The `blocking_lock` call in `compare_with_pending_compact` additionally risks parking a tokio worker thread, which can cascade into broader async-runtime starvation.

---

### Likelihood Explanation

The attacker is an unprivileged P2P peer. The required steps are:

1. Relay enough valid transactions to fill the tx-pool service's inbound channel (or queue enough `SubmitRemoteTx` messages ahead of the `FetchTxs` message).
2. Send a `BlockTransactions` message for a block hash that is already in `pending_compact_blocks` (i.e., the node previously sent a `GetBlockTransactions` to this peer).

Step 2 is a normal protocol flow; the attacker simply responds to a legitimate `GetBlockTransactions` request. Step 1 requires valid transactions but no PoW. The combination is reachable from a standard P2P connection.

---

### Recommendation

Release the `pending_compact_blocks` lock before calling `reconstruct_block`. Clone or extract the data needed from the map entry, drop the guard, perform the async reconstruction, then re-acquire the lock only to update or remove the entry. This is the standard pattern for tokio mutexes: hold the lock only for synchronous map operations, never across I/O awaits.

---

### Proof of Concept

```
1. Connect two peers (A and B) to a test node.
2. Peer A sends a valid CompactBlock with one unresolved short ID → node sends GetBlockTransactions to A.
3. Flood the tx-pool service channel with SubmitRemoteTx messages (valid txs, no PoW needed).
4. Peer A sends BlockTransactions → BlockTransactionsProcess::execute acquires pending_compact_blocks,
   calls reconstruct_block → fetch_txs blocks waiting for the backlogged tx-pool service.
5. Peer B concurrently sends a different CompactBlock.
6. Assert: CompactBlockProcess::execute for peer B does not complete within a 2-second timeout
   (it is blocked on pending_compact_blocks().await).
7. After the tx-pool channel drains, both complete — confirming the stall is lock-induced.
```

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L65-100)
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

**File:** tx-pool/src/service.rs (L346-354)
```rust
    pub async fn fetch_txs(
        &self,
        short_ids: HashSet<ProposalShortId>,
    ) -> Result<HashMap<ProposalShortId, TransactionView>, AnyError> {
        let (responder, response) = tokio::sync::oneshot::channel();
        let request = AsyncRequest::call(short_ids, responder);
        self.sender.send(Message::FetchTxs(request)).await?;
        response.await.map_err(Into::into)
    }
```

**File:** tx-pool/src/service.rs (L844-852)
```rust
        Message::SubmitRemoteTx(Request {
            responder,
            arguments: (tx, declared_cycles, peer),
        }) => {
            let _result = service.submit_remote_tx(tx, declared_cycles, peer).await;
            if let Err(e) = responder.send(()) {
                error!("Responder sending submit_tx result failed {:?}", e);
            };
        }
```

**File:** sync/src/relayer/compact_block_process.rs (L106-107)
```rust
                let mut pending_compact_blocks = shared.state().pending_compact_blocks().await;
                pending_compact_blocks.remove(&block_hash);
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

**File:** sync/src/types/mod.rs (L1332-1332)
```rust
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,
```

**File:** sync/src/types/mod.rs (L1362-1370)
```rust
    pub fn compare_with_pending_compact(&self, hash: &Byte32, now: u64) -> bool {
        let pending = self.pending_compact_blocks.blocking_lock();
        // After compact block request 2s or pending is empty, sync can create tasks
        pending.is_empty()
            || pending
                .get(hash)
                .map(|(_, _, time)| now > time + 2000)
                .unwrap_or(true)
    }
```
