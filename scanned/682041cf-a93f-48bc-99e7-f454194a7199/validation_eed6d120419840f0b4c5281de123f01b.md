### Title
Missing Duplicate Hash Check in `RelayTransactionHashes` Handler Allows Peer to Poison `ask_for_txs` Queue — (File: `sync/src/relayer/transaction_hashes_process.rs`)

---

### Summary
`TransactionHashesProcess::execute()` processes incoming `RelayTransactionHashes` P2P messages without checking for duplicate hashes within the same message. A malicious peer can send up to `MAX_RELAY_TXS_NUM_PER_BATCH` copies of the same unknown hash in one message. The node enqueues all copies into `ask_for_txs`. When the relay timer fires, the node emits a `GetRelayTransactions` message containing those duplicate hashes. The receiving peer — including any honest node — rejects it with `StatusCode::RequestDuplicate`, causing the fetch to fail entirely. This is structurally identical to the reported bug: an object (hash slot) is registered multiple times under the same identifier without a uniqueness check, producing inconsistent downstream behavior.

---

### Finding Description

**Vulnerable handler — no duplicate check:**

`TransactionHashesProcess::execute()` in `sync/src/relayer/transaction_hashes_process.rs`:

```rust
let tx_hashes: Vec<_> = {
    let mut tx_filter = state.tx_filter();
    tx_filter.remove_expired();
    self.message
        .tx_hashes()
        .iter()
        .map(|x| x.to_entity())
        .filter(|tx_hash| !tx_filter.contains(tx_hash))  // only filters *known* hashes
        .collect()
};
state.add_ask_for_txs(self.peer, tx_hashes)  // duplicates enqueued here
```

The `tx_filter` only removes hashes the node has already seen. If the same *unknown* hash appears N times in the message, all N copies pass the filter and are enqueued. [1](#0-0) 

**Sibling handler — duplicate check present:**

`GetTransactionsProcess::execute()` in `sync/src/relayer/get_transactions_process.rs` explicitly rejects duplicate hashes:

```rust
let tx_hashes_set: HashSet<_> = tx_hashes
    .iter()
    .map(|tx_hash| packed::ProposalShortId::from_tx_hash(&tx_hash.to_entity()))
    .collect();

if message_len != tx_hashes_set.len() {
    return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
}
``` [2](#0-1) 

The inconsistency is the root cause: the node correctly rejects *incoming* `GetRelayTransactions` with duplicate hashes, but nothing prevents it from *generating* outgoing `GetRelayTransactions` with duplicate hashes due to a poisoned queue.

**Downstream failure path — `ask_for_txs`:**

When the relay timer fires, `ask_for_txs` drains the queue and sends `GetRelayTransactions` directly from the raw `Vec`, which may contain duplicates:

```rust
for (peer, mut tx_hashes) in self.shared().state().pop_ask_for_txs() {
    if !tx_hashes.is_empty() {
        tx_hashes.truncate(MAX_RELAY_TXS_NUM_PER_BATCH);
        let content = packed::GetRelayTransactions::new_builder()
            .tx_hashes(tx_hashes)   // duplicates forwarded verbatim
            .build();
``` [3](#0-2) 

The unit test for `GetTransactionsProcess` confirms that any peer receiving a `GetRelayTransactions` with duplicate hashes returns `RequestDuplicate` and serves nothing: [4](#0-3) 

---

### Impact Explanation

An attacker sends one `RelayTransactionHashes` message containing `MAX_RELAY_TXS_NUM_PER_BATCH` copies of the same unknown hash. The entire batch slot for that peer is consumed by duplicates. The node's outgoing `GetRelayTransactions` to that peer is rejected with `RequestDuplicate`, so the node fetches zero transactions in that round. By repeating this at the rate-limit boundary the attacker can continuously starve the node's transaction-fetch pipeline for the attacker's peer slot, delaying mempool propagation and potentially blocking time-sensitive transactions (e.g., RBF replacements, channel closures) from reaching the node before a block is mined.

---

### Likelihood Explanation

Any unprivileged connected peer can send `RelayTransactionHashes` messages. No key material, miner role, or special configuration is required. The message is accepted before any authentication beyond the TCP session. The attack is a single crafted P2P message.

---

### Recommendation

Before calling `add_ask_for_txs`, deduplicate the collected hashes in `TransactionHashesProcess::execute()`, mirroring the pattern already used in `GetTransactionsProcess`:

```rust
let tx_hashes: Vec<_> = { ... }.into_iter().collect::<HashSet<_>>().into_iter().collect();
// or: reject the message entirely if duplicates are detected
```

Alternatively, make `add_ask_for_txs` insert into a `HashSet`-backed structure so duplicates are silently dropped at the state layer.

---

### Proof of Concept

1. Connect to a CKB node as a peer supporting `SupportProtocols::RelayV3`.
2. Choose any tx hash `H` that is not in the node's mempool or tx-filter.
3. Craft a `RelayTransactionHashes` message with `tx_hashes = [H; MAX_RELAY_TXS_NUM_PER_BATCH]` (e.g., 100 copies).
4. Send the message. The node passes the length check (`100 ≤ 100`) and the `tx_filter` check (H is unknown), enqueuing 100 copies of H.
5. Wait for the relay timer to fire (`ask_for_txs`). The node sends `GetRelayTransactions { tx_hashes: [H; 100] }` back to the attacker.
6. The attacker (or any honest peer receiving this) returns `StatusCode::RequestDuplicate` — the fetch fails and no transactions are delivered.
7. Repeat to continuously occupy the node's relay fetch bandwidth for this peer slot.

### Citations

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-50)
```rust
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
    }
```

**File:** sync/src/relayer/get_transactions_process.rs (L54-61)
```rust
            let tx_hashes_set: HashSet<_> = tx_hashes
                .iter()
                .map(|tx_hash| packed::ProposalShortId::from_tx_hash(&tx_hash.to_entity()))
                .collect();

            if message_len != tx_hashes_set.len() {
                return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
            }
```

**File:** sync/src/relayer/mod.rs (L606-618)
```rust
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
```

**File:** sync/src/relayer/tests/get_transactions_process.rs (L9-30)
```rust
#[test]
fn test_duplicate() {
    let (_chain, relayer, always_success_out_point) = build_chain(5);

    let tx = new_transaction(&relayer, 1, &always_success_out_point);
    let tx_hash = tx.hash();
    let content = packed::GetRelayTransactions::new_builder()
        .tx_hashes(vec![tx_hash.clone(), tx_hash])
        .build();
    let mock_protocol_context = MockProtocolContext::new(SupportProtocols::RelayV3);
    let nc = Arc::new(mock_protocol_context);
    let peer_index: PeerIndex = 1.into();
    let process = GetTransactionsProcess::new(content.as_reader(), &relayer, nc, peer_index);

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    assert_eq!(
        rt.block_on(process.execute()),
        StatusCode::RequestDuplicate.with_context("Request duplicate transaction")
    );
```
