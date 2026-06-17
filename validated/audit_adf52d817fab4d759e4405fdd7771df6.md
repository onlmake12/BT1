### Title
`fulfilled_requests_cache` Permanently Blocks Retry of Failed Entropy Reveal — (`apps/fortuna/src/keeper/block.rs`)

---

### Summary

The Fortuna keeper inserts a sequence number into `fulfilled_requests_cache` **before** the reveal transaction is confirmed on-chain. If `process_event_with_backoff` exhausts its 5-minute retry window and fails, the sequence number remains in the cache permanently. Every subsequent re-scan of the same block range finds the entry already present and silently skips the event, permanently abandoning the Entropy user's unfulfilled randomness request.

---

### Finding Description

In `process_single_block_batch`, for every event fetched from the chain, the keeper immediately inserts `event.sequence_number` into the shared `fulfilled_requests_cache` (`HashSet<u64>`) and only spawns a processing task if the insert returned `true` (i.e., the sequence number was not already present):

```rust
let newly_inserted = process_params
    .fulfilled_requests_cache
    .write()
    .await
    .insert(event.sequence_number);
if newly_inserted {
    spawn(
        process_event_with_backoff(event.clone(), process_params.clone())
            .in_current_span(),
    );
}
``` [1](#0-0) 

The cache is a plain `HashSet<u64>` that **grows monotonically** — entries are never removed, regardless of whether the spawned task succeeded or failed. [2](#0-1) 

`process_event_with_backoff` calls `submit_tx_with_backoff`, which retries for at most 5 minutes (`max_elapsed_time: Some(Duration::from_secs(300))`): [3](#0-2) 

After exhausting retries, the function logs the failure and returns — but **never removes the sequence number from the cache**: [4](#0-3) 

`watch_blocks` continuously re-scans the last `RETRY_PREVIOUS_BLOCKS = 100` blocks to catch missed events: [5](#0-4) 

When the same block range is re-scanned, the event is fetched again, but `insert()` returns `false` (already present), so no new task is spawned. The request is permanently abandoned.

The misleading comment above `process_single_block_batch` states "If the request was already processed, it will reprocess it" — but the implementation does not support this: there is no eviction or removal path. [6](#0-5) 

---

### Impact Explanation

An Entropy user submits a `requestWithCallback` on-chain. The Fortuna keeper picks up the event, inserts the sequence number into the cache, and spawns a reveal task. If the task fails after 5 minutes (e.g., sustained RPC outage, gas estimation failures, nonce conflicts), the sequence number is permanently locked in the cache. All future re-scans skip the event. The user's `entropyCallback` is never invoked, leaving their application permanently stuck waiting for randomness.

The on-chain request remains active (not cleared), so the user paid the fee but received no service.

---

### Likelihood Explanation

Transient RPC failures, gas price spikes causing estimation failures, and nonce conflicts are routine in production EVM keeper infrastructure. A 5-minute sustained outage — sufficient to exhaust the backoff window — is a realistic operational scenario. No attacker action is required; any Entropy user whose request is processed during such an outage is affected.

---

### Recommendation

Remove the sequence number from `fulfilled_requests_cache` when `process_event_with_backoff` returns an error (i.e., after exhausting retries), so that the next re-scan can spawn a fresh processing task. Alternatively, replace the unbounded `HashSet` with a bounded cache that evicts old entries, or only insert into the cache upon confirmed on-chain success.

---

### Proof of Concept

1. User calls `requestWithCallback` on an Entropy contract; Fortuna keeper observes the `RequestedV2` event for sequence number `N`.
2. `process_single_block_batch` inserts `N` into `fulfilled_requests_cache` and spawns `process_event_with_backoff`.
3. The RPC node becomes unavailable; `submit_tx_with_backoff` retries for 5 minutes and fails.
4. `process_event_with_backoff` returns `Err(...)`. The sequence number `N` remains in `fulfilled_requests_cache`.
5. `watch_blocks` re-scans the last 100 blocks (which include the block containing the request event).
6. `process_single_block_batch` fetches the event again; `fulfilled_requests_cache.insert(N)` returns `false`; no task is spawned.
7. The on-chain request is still active (`get_request_v2` returns `Some`), but the keeper will never attempt to reveal it again. The user's callback is permanently abandoned.

### Citations

**File:** apps/fortuna/src/keeper/block.rs (L47-47)
```rust
    pub fulfilled_requests_cache: Arc<RwLock<HashSet<u64>>>,
```

**File:** apps/fortuna/src/keeper/block.rs (L104-107)
```rust
/// Process a batch of blocks for a chain. It will fetch events for all the blocks in a single call for the provided batch
/// and then try to process them one by one. It checks the `fulfilled_request_cache`. If the request was already fulfilled.
/// It won't reprocess it. If the request was already processed, it will reprocess it.
/// If the process fails, it will retry indefinitely.
```

**File:** apps/fortuna/src/keeper/block.rs (L159-170)
```rust
                    // the write lock guarantees we spawn only one task per sequence number
                    let newly_inserted = process_params
                        .fulfilled_requests_cache
                        .write()
                        .await
                        .insert(event.sequence_number);
                    if newly_inserted {
                        spawn(
                            process_event_with_backoff(event.clone(), process_params.clone())
                                .in_current_span(),
                        );
                    }
```

**File:** apps/fortuna/src/keeper/block.rs (L229-238)
```rust
            let mut from = latest_safe_block.saturating_sub(RETRY_PREVIOUS_BLOCKS);

            // In normal situation, the difference between latest and last safe block should not be more than 2-3 (for arbitrum it can be 10)
            // TODO: add a metric for this in separate PR. We need alerts
            // But in extreme situation, where we were unable to send the block range multiple times, the difference between latest_safe_block and
            // last_safe_block_processed can grow. It is fine to not have the retry mechanisms for those earliest blocks as we expect the rpc
            // to be in consistency after this much time.
            if from > *last_safe_block_processed {
                from = *last_safe_block_processed;
            }
```

**File:** apps/fortuna/src/eth_utils/utils.rs (L155-158)
```rust
    let backoff = ExponentialBackoff {
        max_elapsed_time: Some(Duration::from_secs(300)), // retry for 5 minutes
        ..Default::default()
    };
```

**File:** apps/fortuna/src/keeper/process_event.rs (L255-307)
```rust
        Err(e) => {
            // In case the callback did not succeed, we double-check that the request is still on-chain.
            // If the request is no longer on-chain, one of the transactions we sent likely succeeded, but
            // the RPC gave us an error anyway.
            let req = chain_state
                .contract
                .get_request_v2(event.provider_address, event.sequence_number)
                .await;

            // We only count failures for cases where we are completely certain that the callback failed.
            if req.as_ref().is_ok_and(|x| x.is_some()) {
                tracing::error!("Failed to process event: {}. Request: {:?}", e, req);
                metrics
                    .requests_processed_failure
                    .get_or_create(&account_label)
                    .inc();
                // Do not display the internal error, it might include RPC details.
                let reason = match e {
                    SubmitTxError::GasUsageEstimateError(_, ContractError::Revert(revert)) => {
                        format!("Reverted: {revert}")
                    }
                    SubmitTxError::GasLimitExceeded { limit, estimate } => {
                        format!("Gas limit exceeded: limit = {limit}, estimate = {estimate}")
                    }
                    SubmitTxError::GasUsageEstimateError(_, _) => {
                        "Unable to estimate gas usage".to_string()
                    }
                    SubmitTxError::GasPriceEstimateError(_) => {
                        "Unable to estimate gas price".to_string()
                    }
                    SubmitTxError::SubmissionError(_, _) => {
                        "Error submitting the transaction on-chain".to_string()
                    }
                    SubmitTxError::ConfirmationTimeout(tx) => format!(
                        "Transaction was submitted, but never confirmed. Hash: {}",
                        tx.sighash()
                    ),
                    SubmitTxError::ConfirmationError(tx, _) => format!(
                        "Transaction was submitted, but never confirmed. Hash: {}",
                        tx.sighash()
                    ),
                    SubmitTxError::ReceiptError(tx, _) => {
                        format!("Reveal transaction failed on-chain. Hash: {}", tx.sighash())
                    }
                };
                status.state = RequestEntryState::Failed {
                    reason,
                    provider_random_number: Some(provider_revelation),
                };
                history.add(&status);
            }
        }
    }
```
