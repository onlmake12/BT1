### Title
Missing Count Limit on `RelayTransactions` Message in `TransactionsProcess` Enables Lock-Contention DoS — (File: sync/src/relayer/transactions_process.rs)

---

### Summary

`TransactionsProcess::execute()` iterates over all transactions in a peer-supplied `RelayTransactions` message while holding two `parking_lot::Mutex` locks, with **no count limit check**. Every other relay message handler enforces `MAX_RELAY_TXS_NUM_PER_BATCH` (32 767) before touching shared state. An unprivileged peer can send a crafted `RelayTransactions` message containing far more transactions than were ever requested, causing the node to hold both mutexes for an extended period inside a synchronous call within an async context, stalling the relay executor and blocking transaction propagation.

---

### Finding Description

`TransactionsProcess::execute()` acquires two mutexes and then iterates over every transaction in the message before any count guard:

```rust
// sync/src/relayer/transactions_process.rs  lines 39-57
let txs: Vec<(TransactionView, Cycle)> = {
    let mut tx_filter = shared_state.tx_filter();        // parking_lot::Mutex held
    tx_filter.remove_expired();
    let unknown_tx_hashes = shared_state.unknown_tx_hashes(); // parking_lot::Mutex held

    self.message
        .transactions()
        .iter()
        .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
        .filter(|(tx, _)| {
            !tx_filter.contains(&tx.hash())
                && unknown_tx_hashes
                    .get_priority(&tx.hash())
                    .map(|priority| priority.requesting_peer() == Some(self.peer))
                    .unwrap_or_default()
        })
        .collect()
};
``` [1](#0-0) 

Every other relay handler guards the count first:

| Handler | Guard |
|---|---|
| `TransactionHashesProcess` | `tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` → ban |
| `GetTransactionsProcess` | `message_len > MAX_RELAY_TXS_NUM_PER_BATCH` → ban |
| `GetBlockTransactionsProcess` | `indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` → ban |
| **`TransactionsProcess`** | **no check** | [2](#0-1) [3](#0-2) [4](#0-3) 

`MAX_RELAY_TXS_NUM_PER_BATCH` is defined as 32 767: [5](#0-4) 

`TransactionsProcess::execute()` is called synchronously inside the async `try_process` dispatcher: [6](#0-5) 

Holding a `parking_lot::Mutex` inside an `async fn` without yielding blocks the Tokio executor thread for the entire iteration, stalling every other async task scheduled on that thread.

The `tx_filter` and `unknown_tx_hashes` mutexes are shared across all relay operations: [7](#0-6) 

---

### Impact Explanation

**Impact: Medium**

An attacker sends a `RelayTransactions` message containing more transactions than `MAX_RELAY_TXS_NUM_PER_BATCH`, bounded only by the P2P decompressed-message size limit (added in v0.34.2 as GHSA-3gjh-29fv-8hr6). For each transaction the node calls `to_entity().into_view()` (hash computation + allocation) while holding both mutexes. This blocks:

1. All concurrent `TransactionsProcess` calls from other peers (same `tx_filter` lock).
2. `TransactionHashesProcess` calls — new tx-hash announcements cannot be processed.
3. The `ask_for_txs` timer callback — the node cannot issue `GetRelayTransactions` requests.
4. `send_bulk_of_tx_hashes` — outbound tx-hash broadcasts stall.
5. All other async tasks on the same executor thread.

The result is a temporary but complete stall of relay-layer transaction propagation for the victim node, degrading its ability to relay transactions to and from the rest of the network.

---

### Likelihood Explanation

**Likelihood: Medium**

Any connected peer (no privilege required) can execute this attack:

1. Send `RelayTransactionHashes` with up to 32 767 hashes → node adds them to `unknown_tx_hashes` and sends `GetRelayTransactions`.
2. Respond with a `RelayTransactions` message padded with many extra minimal transactions (each ~100 bytes; a 4 MB message fits ~40 000 entries, exceeding the 32 767 count limit that other handlers enforce).
3. The node iterates all entries while holding both mutexes.

The attacker needs only a single established P2P connection and the ability to craft a valid molecule-encoded `RelayTransactions` message.

---

### Recommendation

Add the same count guard used by every other relay handler at the top of `TransactionsProcess::execute()`:

```rust
// sync/src/relayer/transactions_process.rs
pub fn execute(self) -> Status {
    let message_len = self.message.transactions().len();
    if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "Transactions count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})"
        ));
    }
    // ... existing logic
}
```

This mirrors the pattern already applied in `TransactionHashesProcess`, `GetTransactionsProcess`, and `GetBlockTransactionsProcess`.

---

### Proof of Concept

```
1. Establish a P2P connection to a CKB full node (RelayV3 protocol).

2. Send RelayTransactionHashes with MAX_RELAY_TXS_NUM_PER_BATCH (32767) distinct
   transaction hashes.  The node adds them to unknown_tx_hashes and replies with
   GetRelayTransactions.

3. Construct a RelayTransactions molecule message containing 32767 requested
   transactions PLUS N additional minimal transactions (empty inputs/outputs),
   where N is chosen so the total decompressed size approaches the P2P message
   size limit.  Each minimal transaction is ~100 bytes, so N ≈ 8000–10000 extra
   entries fit within a 4 MB limit.

4. Send the crafted RelayTransactions message.

5. The victim node enters TransactionsProcess::execute(), acquires tx_filter and
   unknown_tx_hashes mutexes, and iterates over all 32767+N entries calling
   to_entity().into_view() (hash computation) on each one while holding both locks.

6. During this window, observe on the victim node:
   - Incoming RelayTransactionHashes from other peers are not processed
     (TransactionHashesProcess blocks on tx_filter).
   - The ask_for_txs timer fires but cannot acquire unknown_tx_hashes.
   - Transaction propagation latency spikes measurably.
``` [8](#0-7) [9](#0-8)

### Citations

**File:** sync/src/relayer/transactions_process.rs (L37-96)
```rust
    pub fn execute(self) -> Status {
        let shared_state = self.relayer.shared().state();
        let txs: Vec<(TransactionView, Cycle)> = {
            // ignore the tx if it's already known or it has never been requested before
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
            let unknown_tx_hashes = shared_state.unknown_tx_hashes();

            self.message
                .transactions()
                .iter()
                .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
        };

        if txs.is_empty() {
            return Status::ok();
        }

        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }

        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

        let tx_pool = self.relayer.shared.shared().tx_pool_controller().clone();
        let peer = self.peer;
        self.relayer
            .shared
            .shared()
            .async_handle()
            .spawn(async move {
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });

        Status::ok()
    }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-35)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/get_transactions_process.rs (L33-39)
```rust
        let message_len = self.message.tx_hashes().len();
        {
            if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
                ));
            }
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-43)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/mod.rs (L60-61)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L177-222)
```rust
    async fn process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) {
        let item_name = message.item_name();
        let item_bytes = message.as_slice().len() as u64;
        let status = self.try_process(Arc::clone(&nc), peer, message).await;

        metric_ckb_message_bytes(
            MetricDirection::In,
            &SupportProtocols::RelayV3.name(),
            message.item_name(),
            Some(status.code()),
            item_bytes,
        );

        if let Some(ban_time) = status.should_ban() {
            error_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, ban {:?} for {}",
                item_name,
                peer,
                ban_time,
                status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, {}",
                item_name,
                peer,
                status
            );
        } else if !status.is_ok() {
            debug_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, {}",
                item_name,
                peer,
                status
            );
        }
    }
```

**File:** sync/src/types/mod.rs (L1016-1028)
```rust
        let state = SyncState {
            shared_best_header,
            tx_filter: Mutex::new(TtlFilter::default()),
            unknown_tx_hashes: Mutex::new(KeyedPriorityQueue::new()),
            peers: Peers::default(),
            pending_get_block_proposals: DashMap::new(),
            pending_compact_blocks: tokio::sync::Mutex::new(HashMap::default()),
            inflight_proposals: DashMap::new(),
            inflight_blocks: RwLock::new(InflightBlocks::default()),
            pending_get_headers: RwLock::new(LruCache::new(GET_HEADERS_CACHE_SIZE)),
            tx_relay_receiver,
            min_chain_work: sync_config.min_chain_work,
        };
```

**File:** util/constant/src/sync.rs (L67-72)
```rust
/// The maximum number transaction hashes inside a `RelayTransactionHashes` message
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
