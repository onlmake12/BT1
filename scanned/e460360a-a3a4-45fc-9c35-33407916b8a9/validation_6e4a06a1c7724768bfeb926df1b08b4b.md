Based on my investigation of the relevant code paths, here is my assessment:

---

### Title
Unbounded Retry Loop for Unresponsive Relay Peers Causes Sustained Outbound Bandwidth Consumption — (`sync/src/relayer/mod.rs`, `sync/src/types/mod.rs`)

### Summary

An unprivileged P2P peer can send a `RelayTransactionHashes` message with up to 32,767 novel hashes and then never respond to subsequent `GetRelayTransactions` requests. The local node will re-request those same hashes every 30 seconds indefinitely, with no retry counter, no peer penalty, and no eviction of stale entries from the unknown-tx queue.

### Finding Description

**Entry point — `TransactionHashesProcess::execute`:**

In `sync/src/relayer/transaction_hashes_process.rs`, the handler accepts up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) hashes, filters out already-known ones via `tx_filter`, and calls `state.add_ask_for_txs(peer, tx_hashes)`. [1](#0-0) 

The `tx_filter` is only checked here — hashes are **not** added to the filter at this point. They are only removed from the unknown queue when the actual transaction is received and verified. If the peer never responds, the hashes remain in the queue permanently. [2](#0-1) 

**Retry timer — `ASK_FOR_TXS_TOKEN` notify:**

The `ASK_FOR_TXS_TOKEN` notify fires every **100 ms**, calling `ask_for_txs`. [3](#0-2) 

`ask_for_txs` calls `pop_ask_for_txs()`, which returns entries whose timeout has expired (set by `RETRY_ASK_TX_TIMEOUT_INCREASE` = 30 s), sends a `GetRelayTransactions` message, and — critically — **re-inserts the hashes with a refreshed timeout**. There is no retry counter, no maximum retry count, and no peer score penalty for non-response. [4](#0-3) 

**The constant:** [5](#0-4) 

**Soft limits do not break the cycle:**

`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767 and `MAX_UNKNOWN_TX_HASHES_SIZE` = 50,000 are explicitly described as **soft limits**. [6](#0-5) 

Even if strictly enforced, they only cap the *size* of the queue, not the number of retries. Once hashes are in the queue, they cycle indefinitely.

**Outbound message size per cycle:**

`ask_for_txs` truncates to `MAX_RELAY_TXS_NUM_PER_BATCH` = 32,767 hashes before sending. [7](#0-6) 

32,767 × 32 bytes ≈ **1 MB per 30 seconds per attacker peer** (~33 KB/s sustained outbound).

### Impact Explanation

- Each attacker peer with 32,767 advertised hashes generates ~1 MB of outbound `GetRelayTransactions` traffic every 30 seconds.
- Multiple attacker peers multiply the effect linearly.
- CPU cost is proportional (serialization, queue operations every 30 s per peer).
- No operator action is required; the cycle runs as long as the peer stays connected.
- Scoped impact: **Low (501–2000)** — sustained bandwidth/CPU degradation, not a crash or fund loss.

### Likelihood Explanation

The attacker only needs to: (1) connect as a normal P2P peer, (2) send one `RelayTransactionHashes` message with 32,767 novel hashes, (3) ignore all subsequent `GetRelayTransactions` messages. No special privileges, no PoW, no keys required.

### Recommendation

1. **Add a per-hash retry counter.** After N retries (e.g., 3–5) without a response, remove the hash from the unknown queue and optionally apply a peer score penalty.
2. **Evict stale entries by age.** Entries older than a configurable TTL (e.g., 5 minutes) should be dropped unconditionally.
3. **Penalize non-responsive peers.** Track `GetRelayTransactions` requests that go unanswered and reduce peer score or disconnect after a threshold.

### Proof of Concept

1. Connect a peer to a CKB node.
2. Send `RelayTransactionHashes` with 32,767 random, novel 32-byte hashes.
3. Never respond to any `GetRelayTransactions` message.
4. Observe outbound traffic: one `GetRelayTransactions` (~1 MB) is sent every 30 seconds to the attacker peer, indefinitely.
5. Assert that after 10 minutes (20 cycles), the outbound message count is still 1 per 30 s with no decay — confirming the unbounded retry loop.

### Citations

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-49)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
        }

        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };

        state.add_ask_for_txs(self.peer, tx_hashes)
```

**File:** sync/src/relayer/mod.rs (L605-628)
```rust
    pub async fn ask_for_txs(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        for (peer, mut tx_hashes) in self.shared().state().pop_ask_for_txs() {
            if !tx_hashes.is_empty() {
                debug_target!(
                    crate::LOG_TARGET_RELAY,
                    "Send get transaction ({} hashes) to {}",
                    tx_hashes.len(),
                    peer,
                );
                tx_hashes.truncate(MAX_RELAY_TXS_NUM_PER_BATCH);
                let content = packed::GetRelayTransactions::new_builder()
                    .tx_hashes(tx_hashes)
                    .build();
                let message = packed::RelayMessage::new_builder().set(content).build();
                let status = async_send_message_to(nc, peer, &message).await;
                if !status.is_ok() {
                    ckb_logger::error!(
                        "interrupted request for transactions, status: {:?}",
                        status
                    );
                }
            }
        }
    }
```

**File:** sync/src/relayer/mod.rs (L801-802)
```rust
        nc.set_notify(Duration::from_millis(100), ASK_FOR_TXS_TOKEN)
            .await
```

**File:** util/constant/src/sync.rs (L57-57)
```rust
pub const RETRY_ASK_TX_TIMEOUT_INCREASE: Duration = Duration::from_secs(30);
```

**File:** util/constant/src/sync.rs (L69-71)
```rust
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
```
