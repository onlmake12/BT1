### Title
Batch Transaction Relay Aborts Entirely When Any Single Transaction Has Excessive Declared Cycles — (`File: sync/src/relayer/transactions_process.rs`)

---

### Summary

In `TransactionsProcess::execute()`, when a `RelayTransactions` P2P message contains a batch of transactions, the code checks whether **any** transaction in the batch has `declared_cycles > max_block_cycles`. If even one transaction fails this check, the **entire batch is silently dropped** and all other valid transactions in the batch are never submitted to the tx pool. A malicious peer can exploit this to selectively suppress propagation of specific valid transactions to a target node.

---

### Finding Description

In `sync/src/relayer/transactions_process.rs`, `TransactionsProcess::execute()` processes a batch of relayed transactions:

```rust
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
    return Status::ok();  // <-- entire batch dropped here
}
``` [1](#0-0) 

The `.any()` predicate causes a single offending transaction to abort processing of the entire batch. The valid transactions that follow it in the same message are never passed to `submit_remote_tx` and are never marked as known via `mark_as_known_txs`. [2](#0-1) 

The `RelayTransactions` message can carry up to `MAX_RELAY_TXS_NUM_PER_BATCH` transactions in a single message. [3](#0-2) 

---

### Impact Explanation

A malicious peer that knows which transaction hashes a target node has requested (observable from `GetRelayTransactions` messages) can craft a `RelayTransactions` response that bundles those requested transactions together with one transaction whose `declared_cycles` field is set to a value exceeding `max_block_cycles`. The receiving node will:

1. Ban the sending peer (correct behavior).
2. Drop **all** valid transactions in the batch without submitting them to the tx pool (incorrect behavior).

Since the transactions are not marked as known and the peer is banned, the node must wait to re-request those transactions from a different peer. This delays or, in a targeted network partition scenario, prevents propagation of specific transactions to specific nodes, degrading tx-pool liveness and potentially delaying confirmation of time-sensitive transactions. [4](#0-3) 

---

### Likelihood Explanation

The attack requires only an unprivileged P2P peer. The attacker must:
1. Connect to the target node.
2. Announce transaction hashes (via `RelayTransactionHashes`) to cause the node to issue a `GetRelayTransactions` request.
3. Respond with a `RelayTransactions` batch that includes the requested valid transactions plus one transaction with an inflated `declared_cycles` value.

This is fully within the capability of any peer on the network. The `declared_cycles` field is a plain integer in the wire message and is not cryptographically bound to the transaction content at the relay layer — the mismatch is only detected later inside `_process_tx` via `Reject::DeclaredWrongCycles`. [5](#0-4) 

---

### Recommendation

Replace the batch-aborting `.any()` check with a per-transaction filter that removes only the offending transaction(s) from the batch, bans the peer, and continues processing the remaining valid transactions:

```rust
let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
let has_excessive = txs
    .iter()
    .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles);

if has_excessive {
    self.nc.ban_peer(
        self.peer,
        DEFAULT_BAN_TIME,
        String::from("relay declared cycles greater than max_block_cycles"),
    );
    // Filter out offending txs but continue with the rest
}

let txs: Vec<_> = txs
    .into_iter()
    .filter(|(_, declared_cycles)| declared_cycles <= &max_block_cycles)
    .collect();

if txs.is_empty() {
    return Status::ok();
}
// ... proceed to mark_as_known_txs and submit
```

---

### Proof of Concept

1. Attacker peer connects to a CKB node.
2. Attacker sends `RelayTransactionHashes` announcing hashes `[H_valid_1, H_valid_2, H_bad]`.
3. Node responds with `GetRelayTransactions` for those hashes.
4. Attacker sends `RelayTransactions` with:
   - `tx_1` (valid, correct cycles)
   - `tx_2` (valid, correct cycles)
   - `tx_bad` (any transaction body, but `declared_cycles` field set to `max_block_cycles + 1`)
5. `TransactionsProcess::execute()` reaches line 64, `.any()` returns `true` for `tx_bad`.
6. Node bans the peer and returns at line 73 — `tx_1` and `tx_2` are never submitted to the tx pool.
7. Node must re-request `tx_1` and `tx_2` from other peers, introducing propagation delay. [6](#0-5)

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

**File:** util/types/src/core/tx_pool.rs (L44-46)
```rust
    /// Declared wrong cycles
    #[error("Declared wrong cycles {0}, actual {1}")]
    DeclaredWrongCycles(Cycle, Cycle),
```
