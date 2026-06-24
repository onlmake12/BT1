All code references verified against the actual repository. Here is the validation result:

---

Audit Report

## Title
Unbounded Inline BFS Orphan Processing in `process_orphan_tx` Stalls Verify Workers — (File: tx-pool/src/process.rs)

## Summary
`process_orphan_tx` performs an unbounded BFS traversal over the orphan pool with no per-call iteration cap. For orphans whose declared cycle is ≤ `max_tx_verify_cycles`, `_process_tx` is called inline with `command_rx: None` (non-pausable), blocking the verify worker for the entire chain traversal. An unprivileged P2P peer can pre-fill the orphan pool with up to 100 chained orphans and trigger this cascade by submitting the root transaction, stalling all verify workers.

## Finding Description
`process_orphan_tx` (L591–671, `tx-pool/src/process.rs`) runs an unbounded `while let Some(previous) = orphan_queue.pop_front()` loop. For each orphan with `cycle ≤ max_tx_verify_cycles`, it calls `_process_tx(orphan.tx.clone(), Some(orphan.cycle), None)` at L626 — the `None` third argument means no `command_rx` is passed, disabling the pause/resume mechanism. On success, the resolved orphan is pushed back into `orphan_queue` at L641, cascading through the entire chain. The orphan pool cap is `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` (orphan.rs L16), enforced by eviction at L119 (`while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS`), but the BFS loop has no corresponding per-call bound. By contrast, orphans with `cycle > max_tx_verify_cycles` are correctly re-enqueued asynchronously via `enqueue_verify_queue` at L605–624. The verify worker in `verify_mgr.rs` (L147–158) calls `_process_tx` with `Some(&mut self.command_rx)` (pausable) for queue entries, then calls `after_process` → `process_orphan_tx` synchronously before returning to pick the next entry — meaning the worker is occupied for the entire orphan chain traversal with no opportunity to interleave other queued transactions.

## Impact Explanation
A single verify worker is blocked from processing any other `VerifyQueue` entries for the duration of the unbounded orphan cascade — up to 100 inline `_process_tx` calls, each performing full script execution with no pause/suspend mechanism. With scripts consuming close to `max_tx_verify_cycles` (70,000,000 cycles, `resource/ckb.toml` L215), the stall is significant. The default worker count is `max(num_cpus * 3/4, 1)` (`util/app-config/src/configs/tx_pool.rs` L47); an attacker can repeat the attack to stall all workers simultaneously. This constitutes **CKB network congestion with few costs** — a valid High-severity bounty impact.

## Likelihood Explanation
The attack requires only an unprivileged P2P peer with a single live cell to fund the root transaction. Constructing 100 chained orphan transactions is low-cost. The orphan pool's random eviction policy does not prevent a linear chain from being retained if submitted in reverse order (each orphan's input is missing, so each is accepted). The attack is repeatable: after the orphan pool drains, the attacker can immediately refill and trigger another cascade.

## Recommendation
Introduce a per-call iteration limit to `process_orphan_tx`. Process at most `N` orphans per invocation (e.g., `N = 10`) and re-schedule remaining work asynchronously via `enqueue_verify_queue` or a deferred task. Orphans with declared cycle ≤ `max_tx_verify_cycles` should also be re-enqueued to the verify queue (as is already done for the `cycle > max_tx_verify_cycles` branch at L598–624) rather than processed inline, so the verify worker's pause/resume mechanism (`command_rx`) remains active and the worker can interleave other queued transactions.

## Proof of Concept
1. Connect to a CKB node as a P2P peer. Own one live cell as `cell[0]`.
2. Construct a linear chain of 100 transactions: `tx[0]` spends `cell[0]`; `tx[i]` spends output 0 of `tx[i-1]` for `i = 1..99`. Each `tx[i]` for `i ≥ 1` carries a lock script consuming ~70,000,000 cycles. Declare `cycle = max_tx_verify_cycles` for each.
3. Submit `tx[1]` through `tx[99]` via P2P relay. Each is added to the orphan pool (input missing). The pool fills to 100 entries (orphan.rs L119–125 evicts only when `len > 100`).
4. Submit `tx[0]`. It passes pre-check and enters the verify queue normally.
5. The verify worker picks up `tx[0]`, verifies it (pausable, with `command_rx`), calls `after_process` → `process_orphan_tx(&tx[0])`.
6. `process_orphan_tx` enters the BFS loop: finds `tx[1]`, calls `_process_tx(tx[1], Some(70M_cycles), None)` (non-pausable), succeeds, pushes `tx[1]` into `orphan_queue`; finds `tx[2]`, calls `_process_tx(tx[2], ...)`, and so on through all 100 orphans — all within a single invocation, with no iteration cap (process.rs L595–670).
7. The verify worker is blocked for the entire duration. All other transactions in the `VerifyQueue` are delayed until the loop completes. Repeat from step 2 to stall additional workers. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/process.rs (L591-598)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
            for orphan in orphans.into_iter() {
                if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
```

**File:** tx-pool/src/process.rs (L625-641)
```rust
                } else if let Some((ret, _snapshot)) = self
                    ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)
                    .await
                {
                    match ret {
                        Ok(_) => {
                            self.send_result_to_relayer(TxVerificationResult::Ok {
                                original_peer: Some(orphan.peer),
                                tx_hash: orphan.tx.hash(),
                            });
                            debug!(
                                "process_orphan {} success, find previous from {}",
                                orphan.tx.hash(),
                                tx.hash()
                            );
                            self.remove_orphan_tx(&orphan.tx.proposal_short_id()).await;
                            orphan_queue.push_back(orphan.tx);
```

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```

**File:** tx-pool/src/verify_mgr.rs (L147-158)
```rust
            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
```

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```
