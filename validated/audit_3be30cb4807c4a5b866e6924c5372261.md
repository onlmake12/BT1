### Title
Shared `VerifyQueue` Exhaustion via Fee-less Transactions Blocks All Legitimate Transaction Submissions — (File: tx-pool/src/component/verify_queue.rs)

---

### Summary

The `VerifyQueue` is a shared, global pre-verification buffer with a hardcoded 256 MB size limit. Fee validation is deferred until after a transaction is dequeued for verification — not at admission time. An unprivileged attacker (RPC caller or P2P peer) can cheaply fill the queue with large, fee-less transactions that pass non-contextual format checks, causing all subsequent legitimate transaction submissions to be rejected with `Reject::Full` for as long as the attacker sustains the flood.

This is the direct CKB analog of the GMX/PirexGmx cooldown vulnerability: a shared, global resource (the verify queue) can be kept permanently saturated by cheap operations, blocking all users from submitting transactions — just as PirexGmx's shared GLP position could be kept in cooldown by cheap deposits, blocking all redemptions.

---

### Finding Description

**Root cause — admission without fee gating**

`VerifyQueue` (`tx-pool/src/component/verify_queue.rs`) is the single shared pre-verification buffer for every incoming transaction, regardless of origin (RPC or P2P relay). Its only admission guard is a size check against a hardcoded constant: [1](#0-0) [2](#0-1) 

When `is_full` returns `true`, `add_tx` immediately returns `Reject::Full`: [3](#0-2) 

**Fee validation is deferred — it happens only after dequeue**

The admission path `resumeble_process_tx` calls only `non_contextual_verify` (pure format checks, no chain-state access, no fee check) before calling `enqueue_verify_queue`: [4](#0-3) 

Fee validation (`check_tx_fee`) is invoked only later, inside `pre_check`, which is called by `_process_tx` after the transaction is dequeued by `VerifyMgr`. By that point the queue slot has already been consumed. [5](#0-4) 

**Attack path**

An attacker constructs transactions that:
- Are structurally valid (pass `non_contextual_verify`)
- Reference non-existent or already-spent cells (contextual check, deferred)
- Carry zero fees (fee check, deferred)
- Are as large as possible — up to `TRANSACTION_SIZE_LIMIT = 512 KB` [6](#0-5) 

Approximately 512 such transactions fill the 256 MB queue. The attacker submits them continuously via the `send_transaction` RPC (no rate limit) or via P2P relay (30 req/s per peer, bypassable with multiple connections). As the `VerifyMgr` drains and rejects them (with `Reject::LowFeeRate`), the attacker re-fills the queue, keeping it perpetually saturated.

**Shared-resource contention — the GMX analog**

| GMX / PirexGmx | CKB |
|---|---|
| `GlpManager` per-user cooldown (15 min) | `VerifyQueue` global size cap (256 MB) |
| Any Pirex deposit resets the cooldown for all users | Any large fee-less tx consumes queue capacity for all submitters |
| Redemptions blocked while cooldown is active | `send_transaction` / relay submissions blocked while queue is full |
| Attacker cost: ~$9.60/day for 1-wei deposits | Attacker cost: bandwidth only; no on-chain fee required at admission |

The `VerifyQueue` is the single shared entity through which every transaction must pass,

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L215-220)
```rust
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
```

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```
