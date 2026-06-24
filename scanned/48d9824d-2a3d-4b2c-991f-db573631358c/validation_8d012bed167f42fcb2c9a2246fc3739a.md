Audit Report

## Title
Missing `MAX_RELAY_TXS_NUM_PER_BATCH` Count Check in `TransactionsProcess::execute()` Enables CPU Exhaustion via Oversized `RelayTransactions` Message — (`sync/src/relayer/transactions_process.rs`)

## Summary
`TransactionsProcess::execute()` contains no guard on the number of transactions in an incoming `RelayTransactions` message, while both sibling handlers (`TransactionHashesProcess` and `GetTransactionsProcess`) enforce `MAX_RELAY_TXS_NUM_PER_BATCH = 32767`. An unprivileged relay peer can send a single `RelayTransactions` message containing an arbitrarily large number of transactions, forcing the node to deserialize every entry via `.map(|tx| tx.transaction().to_entity().into_view())` before the `unknown_tx_hashes` filter discards the excess. This constitutes a CPU-exhaustion vector reachable from any connected peer with a single message.

## Finding Description
The relay pipeline enforces the batch limit at two of three stages:

- `transaction_hashes_process.rs` line 29: `if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` — **check present** [1](#0-0) 

- `get_transactions_process.rs` line 35: `if message_len > MAX_RELAY_TXS_NUM_PER_BATCH` — **check present** [2](#0-1) 

- `transactions_process.rs` lines 37–57: **no count check**; execution proceeds directly to the iterator chain [3](#0-2) 

The iterator chain is:
```rust
self.message
    .transactions()
    .iter()
    .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
    .filter(|(tx, _)| { ... unknown_tx_hashes check ... })
    .collect()
```

In Rust's lazy iterator model, `.map()` executes **before** `.filter()` for each element. This means `tx.transaction().to_entity().into_view()` — a full molecule deserialization into an owned `TransactionView` — is called for **every** transaction in the message, regardless of whether it passes the filter. There is no count guard before this loop begins.

The `unknown_tx_hashes` filter does bound the size of the resulting `Vec` (capped by `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767`), so **memory** allocated in the output is bounded. However, **CPU cost** is proportional to the total number of transactions in the incoming message, which has no enforced upper bound on the receiving side. [4](#0-3) 

The rate limiter in `mod.rs` (30 req/s per peer+message-type) limits message frequency but not message size, so one large message per rate window is sufficient. [5](#0-4) 

No network-level incoming message size cap was found in the relay handler code.

## Impact Explanation
An attacker can force the victim node to perform unbounded deserialization work (CPU) on a single `RelayTransactions` message. With `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` and no receiving-side size limit, a message containing hundreds of thousands of fabricated transactions causes proportional CPU consumption on the node's relay processing thread. Sustained or repeated attacks (within the rate limit) can degrade or crash a CKB node. This matches the allowed impact: **"Vulnerabilities which could easily crash a CKB node" — High (10001–15000 points)**.

## Likelihood Explanation
The attack requires only a standard relay peer connection — no keys, no hash power, no privilege. The attacker controls the content of `RelayTransactions` messages entirely. Pre-staging via `RelayTransactionHashes` is optional; the attacker can send a `RelayTransactions` message with arbitrary hashes at any time. The `unknown_tx_hashes` filter will discard most entries, but deserialization of all entries still occurs. The rate limiter (30/s) does not prevent a single large message from being processed. The attack is repeatable and requires minimal resources from the attacker.

## Recommendation
Add the same count guard at the top of `TransactionsProcess::execute()` that exists in the other two handlers:

```rust
pub fn execute(self) -> Status {
+   let tx_count = self.message.transactions().len();
+   if tx_count > MAX_RELAY_TXS_NUM_PER_BATCH {
+       return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
+           "RelayTransactions count({tx_count}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
+       ));
+   }
    let shared_state = self.relayer.shared().state();
    ...
```

Also import `MAX_RELAY_TXS_NUM_PER_BATCH` and `StatusCode` in `transactions_process.rs`, mirroring the pattern in `transaction_hashes_process.rs`. [6](#0-5) 

## Proof of Concept
1. Connect to a CKB node as a relay peer.
2. (Optional pre-staging) Send `RelayTransactionHashes` messages with up to 32767 distinct hashes to populate `unknown_tx_hashes` for your peer.
3. Construct a `RelayTransactions` message containing `N >> MAX_RELAY_TXS_NUM_PER_BATCH` entries (e.g., 500,000 minimal transactions with fabricated hashes). Send it.
4. `TransactionsProcess::execute()` enters the iterator chain at line 45 with no count guard. `.map()` calls `to_entity().into_view()` for all 500,000 entries before `.filter()` discards those not in `unknown_tx_hashes`.
5. Observe sustained CPU spike on the node's relay processing thread. Repeat once per rate-limit window to maintain pressure.

A unit test can be written by constructing a `packed::RelayTransactions` with `N > MAX_RELAY_TXS_NUM_PER_BATCH` entries, calling `TransactionsProcess::new(...).execute()`, and asserting that the call returns `StatusCode::ProtocolMessageIsMalformed` (currently it does not — it returns `Status::ok()` after iterating all entries). [3](#0-2)

### Citations

**File:** sync/src/relayer/transaction_hashes_process.rs (L1-2)
```rust
use crate::relayer::{MAX_RELAY_TXS_NUM_PER_BATCH, Relayer};
use crate::{Status, StatusCode};
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

**File:** sync/src/relayer/get_transactions_process.rs (L35-39)
```rust
            if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
                ));
            }
```

**File:** sync/src/relayer/transactions_process.rs (L37-57)
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
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
