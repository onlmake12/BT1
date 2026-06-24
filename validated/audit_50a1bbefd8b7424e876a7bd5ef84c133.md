Audit Report

## Title
Unbounded Linear Scan in `estimate_fee_rate` Fallback Enables RPC-Triggered Pool Service Starvation — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::estimate_fee_rate` performs an O(n) scan over every entry in the tx-pool with no iteration cap. Because the tx-pool service processes messages sequentially in a single async loop, an attacker who fills the pool with minimum-fee transactions and then spams the `estimate_fee_rate` RPC can stall the pool service's message queue, delaying transaction admission, block assembly, and other pool operations for the duration of each scan. No authentication or rate-limiting guards this path, and the fallback is enabled by default.

## Finding Description
`PoolMap::estimate_fee_rate` at `tx-pool/src/component/pool_map.rs` L334–359 iterates the full score-sorted entry set unconditionally:

```rust
let iter = self.entries.iter_by_score().rev();   // no cap
for entry in iter {
    current_block_bytes += entry.inner.size;
    current_block_cycles += entry.inner.cycles;
    if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
        target_blocks -= 1;
        if target_blocks == 0 { return entry.inner.fee_rate(); }
        ...
    }
}
min_fee_rate   // reached after scanning every entry
```

The only early-exit requires accumulating `target_blocks × max_block_bytes` worth of entries. An attacker who packs the pool with minimum-size (~100 B), minimum-fee transactions ensures each entry contributes negligible bytes, so the loop exhausts the entire pool before the threshold is crossed even once, falling through to `return min_fee_rate` after a full scan. [1](#0-0) 

The tx-pool service is a single async message loop. Every message — including `submit_transaction`, block assembly, and `estimate_fee_rate` — is processed one at a time. A slow `estimate_fee_rate` handler directly delays all subsequent queued messages. [2](#0-1) 

The RPC implementation defaults `enable_fallback = true`, making the pool-scan path reachable by any caller without special parameters: [3](#0-2) 

A parallel unbounded issue exists in `get_all_entry_info`, which collects every pending, gap, and proposed entry into heap-allocated maps with no pagination: [4](#0-3) 

No rate-limiting, per-call entry cap, or authentication is applied to either endpoint.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker who fills the pool with minimum-fee transactions (cheap, bounded by `max_tx_pool_size`) and then spams `estimate_fee_rate` in a tight loop can continuously stall the pool service's message queue. This delays transaction propagation (new transactions queue behind each scan), degrades block-assembly latency (the block assembler also sends messages to the pool service), and can cause legitimate RPC callers to time out — all at negligible ongoing cost to the attacker once the pool is filled. [5](#0-4) 

## Likelihood Explanation
- `estimate_fee_rate` is publicly documented, enabled by default, and requires no authentication.
- `enable_fallback` defaults to `true`; no special parameter is needed to trigger the scan path.
- Filling the pool with minimum-fee transactions is cheap: attacker cost scales with `max_tx_pool_size / min_tx_size`, while disruption per RPC call scales with the same quantity.
- No connection-level or per-IP rate limiting is enforced at the RPC layer for this endpoint.
- The attack is repeatable and stateless: the attacker does not need to maintain any persistent state beyond keeping the pool full. [6](#0-5) 

## Recommendation
1. **Cap the scan in `estimate_fee_rate`**: introduce a `max_entries` bound proportional to `target_blocks × expected_txs_per_block` and break early:
   ```rust
   let max_entries = target_blocks * MAX_BLOCK_PROPOSALS_LIMIT;
   for entry in iter.take(max_entries) { ... }
   ```
2. **Paginate `get_all_entry_info` / `get_raw_tx_pool`**: add an optional `limit`/`cursor` parameter to prevent full-pool serialisation in a single call.
3. **Rate-limit `estimate_fee_rate` and `get_raw_tx_pool`** at the RPC server layer (e.g., per-IP token bucket) to bound aggregate scan rate. [1](#0-0) 

## Proof of Concept
1. Submit `N` transactions of minimum size (~100 B each) and minimum fee rate until the pool reaches `max_tx_pool_size`. Each transaction is valid and accepted.
2. In a tight loop, call `estimate_fee_rate` with no arguments (defaults: `estimate_mode = null`, `enable_fallback = true`).
3. Because each transaction contributes ~100 B and `max_block_bytes` is ~500 KB, the loop visits ~5,000 entries before the byte threshold is crossed once. With `target_blocks = 2` (medium priority), ~10,000 entries are visited per call — or the entire pool if the threshold is never crossed.
4. Observe that concurrent `submit_transaction` calls experience increasing latency or timeout, proportional to the number of in-flight `estimate_fee_rate` calls queued in the service channel.
5. Confirm by instrumenting the service loop: each `EstimateFeeRate` message handler holds the CPU for the full scan duration before the next message is dequeued. [2](#0-1) [5](#0-4)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L334-359)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
    }
```

**File:** tx-pool/src/service.rs (L1000-1006)
```rust
        Message::GetAllEntryInfo(Request { responder, .. }) => {
            let tx_pool = service.tx_pool.read().await;
            let info = tx_pool.get_all_entry_info();
            if let Err(e) = responder.send(info) {
                error!("Responder sending get_all_entry_info failed {:?}", e)
            };
        }
```

**File:** rpc/src/module/experiment.rs (L301-315)
```rust
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64> {
        let estimate_mode = estimate_mode.unwrap_or_default();
        let enable_fallback = enable_fallback.unwrap_or(true);
        self.shared
            .tx_pool_controller()
            .estimate_fee_rate(estimate_mode.into(), enable_fallback)
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?
            .map_err(RPCError::from_any_error)
            .map(core::FeeRate::as_u64)
            .map(Into::into)
    }
```

**File:** tx-pool/src/pool.rs (L464-487)
```rust
    pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
        let pending = self
            .pool_map
            .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let proposed = self
            .pool_map
            .sorted_proposed_iter()
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let conflicted = self
            .conflicts_cache
            .iter()
            .map(|(_id, tx)| tx.hash())
            .collect();
        TxPoolEntryInfo {
            pending,
            proposed,
            conflicted,
        }
    }
```
