Based on my code trace, the vulnerability path is real and concrete. Here is my analysis:

---

### Title
Unbounded RPC-Triggered Relay Amplification via Duplicate `send_transaction` — (`rpc/src/module/pool.rs`, `tx-pool/src/process.rs`, `sync/src/relayer/mod.rs`)

---

### Summary

An unprivileged caller with RPC access can repeatedly submit the same already-pooled transaction via `send_transaction`. Each call causes the tx-pool service to enqueue a `TxVerificationResult::Ok` relay event, which the relayer then broadcasts as a `RelayTransactionHashes` message to **every connected peer** on the next timer tick. There is no rate limiting on this path at the RPC layer, the tx-pool layer, or the outbound relay layer.

---

### Finding Description

**Step 1 — RPC entry point (`rpc/src/module/pool.rs:612-634`):**

`send_transaction` unconditionally calls `tx_pool.submit_local_tx(tx.clone())` with no rate limiting or deduplication guard. [1](#0-0) 

**Step 2 — Message dispatch (`tx-pool/src/service.rs:805-812`):**

`Message::SubmitLocalTx` is handled by calling `service.process_tx(tx, None).await`, where `None` means `remote = None` (local origin). [2](#0-1) 

**Step 3 — Critical branch in `after_process` (`tx-pool/src/process.rs:538-544`):**

When the tx is already in the pool (`Reject::Duplicated`) and the submission is local (`remote = None`), the code **explicitly re-enqueues a relay event** as if the tx were freshly accepted:

```rust
Err(Reject::Duplicated(_)) => {
    debug!("after_process {} duplicated", tx_hash);
    // re-broadcast tx when it's duplicated and submitted through local rpc
    self.send_result_to_relayer(TxVerificationResult::Ok {
        original_peer: None,
        tx_hash,
    });
}
``` [3](#0-2) 

**Step 4 — Relay broadcast to all peers (`sync/src/relayer/mod.rs:631-706`):**

`send_bulk_of_tx_hashes` drains the relay result queue and, for every `TxVerificationResult::Ok { original_peer: None, tx_hash }`, pushes `tx_hash` into the send list for **every connected peer** with no check against `known_txs` before insertion. `mark_as_known_tx` is called only after the hash is already added to `selected`, so it does not prevent the current or future broadcasts of the same hash. [4](#0-3) 

**Step 5 — No outbound rate limiting:**

The rate limiter in the relayer is keyed by `(peer, message.item_id())` and applies exclusively to **incoming** P2P messages from remote peers. It does not apply to outbound broadcasts triggered by local RPC calls. [5](#0-4) 

The RPC documentation itself confirms this is intentional behavior: *"If the transaction is already in the pool, rebroadcast it to peers."* [6](#0-5) 

---

### Impact Explanation

Each call to `send_transaction(same_tx)` enqueues one `TxVerificationResult::Ok` entry. On each `TX_HASHES_TOKEN` timer tick, `send_bulk_of_tx_hashes` drains up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) entries and sends a `RelayTransactionHashes` P2P message to each of the N connected peers. The amplification factor is **O(calls × peers)** outbound P2P messages generated at near-zero cost (no PoW, no fee, no new transaction needed). This can saturate the relay protocol channel and cause network congestion across the CKB P2P network. [7](#0-6) 

---

### Likelihood Explanation

Any process with access to the RPC endpoint can trigger this. While the RPC defaults to localhost, many production deployments (dApp backends, exchanges, mining pools) expose it to internal networks or the internet. The attack requires only one valid transaction already in the pool and a loop of JSON-RPC calls — no keys, no PoW, no special privileges beyond RPC access.

---

### Recommendation

1. **Rate-limit the rebroadcast path**: Before enqueuing `TxVerificationResult::Ok` for a `Reject::Duplicated` case, check a per-tx timestamp and suppress rebroadcast if the same tx was rebroadcast within a configurable cooldown window (e.g., 30 seconds).
2. **Add RPC-level rate limiting**: Apply per-IP or per-connection rate limiting to `send_transaction` calls in `ServiceBuilder::enable_pool`.
3. **Deduplicate the relay queue**: In `send_bulk_of_tx_hashes`, check `known_txs` before adding a hash to `selected` to prevent redundant broadcasts.

---

### Proof of Concept

```python
import requests, json

RPC = "http://127.0.0.1:8114"
TX = { ... }  # any valid tx already accepted into the pool

def rpc(method, params):
    return requests.post(RPC, json={"id":1,"jsonrpc":"2.0","method":method,"params":params}).json()

# First call: accepted into pool
rpc("send_transaction", [TX, "passthrough"])

# Subsequent calls: each triggers a rebroadcast to all N peers
for _ in range(1000):
    rpc("send_transaction", [TX, "passthrough"])

# On a connected peer, capture RelayTransactionHashes messages.
# Expected: ~1000 * N messages received, one per call per peer.
```

Each iteration hits `Reject::Duplicated` → `send_result_to_relayer(Ok)` → `send_bulk_of_tx_hashes` → `RelayTransactionHashes` to all peers, with no throttle.

### Citations

**File:** rpc/src/module/pool.rs (L21-22)
```rust
    /// Submits a new transaction into the transaction pool. If the transaction is already in the
    /// pool, rebroadcast it to peers.
```

**File:** rpc/src/module/pool.rs (L612-634)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
```

**File:** tx-pool/src/service.rs (L805-812)
```rust
        Message::SubmitLocalTx(Request {
            responder,
            arguments: tx,
        }) => {
            let result = service.process_tx(tx, None).await.map(|_| ());
            if let Err(e) = responder.send(result) {
                error!("Responder sending submit_tx result failed {:?}", e);
            };
```

**File:** tx-pool/src/process.rs (L538-544)
```rust
                    Err(Reject::Duplicated(_)) => {
                        debug!("after_process {} duplicated", tx_hash);
                        // re-broadcast tx when it's duplicated and submitted through local rpc
                        self.send_result_to_relayer(TxVerificationResult::Ok {
                            original_peer: None,
                            tx_hash,
                        });
```

**File:** sync/src/relayer/mod.rs (L89-98)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
```

**File:** sync/src/relayer/mod.rs (L662-668)
```rust
                                None => {
                                    // since this tx is submitted through local rpc, it is assumed to be a new tx for all connected peers
                                    let hashes = selected
                                        .entry(*target)
                                        .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                    hashes.push(tx_hash.clone());
                                    self.shared.state().mark_as_known_tx(tx_hash.clone());
```

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```
