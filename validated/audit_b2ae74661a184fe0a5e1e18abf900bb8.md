### Title
`max_tx_verify_cycles` Per-Transaction Cycle Cap Bypassed via Relay Path — (File: `tx-pool/src/process.rs`, `sync/src/relayer/transactions_process.rs`, `tx-pool/src/component/verify_queue.rs`)

---

### Summary

The `TxPoolConfig::max_tx_verify_cycles` field is documented as causing the tx-pool to reject transactions whose cycles exceed the configured limit. However, the relay transaction processing path does not enforce this limit. Instead, it uses the peer-declared cycle count (bounded only by the consensus `max_block_cycles`) as the per-transaction verification ceiling. A remote peer can relay transactions with declared cycles up to `max_block_cycles` (3.5 billion), bypassing the intended per-transaction cap entirely.

---

### Finding Description

**The documented limit:**

`TxPoolConfig::max_tx_verify_cycles` is documented as:

> "tx pool rejects txs that cycles greater than max_tx_verify_cycles" [1](#0-0) 

The CHANGELOG also states the purpose: "Support limit `max_tx_verify_cycles` — The purpose is to limit max verify cycles on single tx, to reduce DDOS vulnerability." [2](#0-1) 

**The actual enforcement — only a scheduling threshold:**

`max_tx_verify_cycles` is passed to `VerifyQueue::new()` as `large_cycle_threshold`. It classifies transactions as "large cycle" for scheduling priority, but does **not** reject them: [3](#0-2) 

**The relay path bypasses the limit:**

In `_process_tx`, the verification cycle limit is set to `declared_cycles` (from the peer) or `max_block_cycles` — never `max_tx_verify_cycles`: [4](#0-3) 

For relayed transactions, `declared_cycles` is `Some(peer_value)`. The only check on `declared_cycles` in the relay handler is that it does not exceed `max_block_cycles`: [5](#0-4) 

So a peer can declare cycles = `max_block_cycles - 1` (≈ 3.499 billion), and the node will run the CKB-VM for up to that many cycles verifying the transaction — even if the operator set `max_tx_verify_cycles` to, say, 70 million (the default: `TWO_IN_TWO_OUT_CYCLES * 20`). [6](#0-5) 

**The `DeclaredWrongCycles` check forces the attacker to actually run the cycles:**

After verification, if `declared != verified.cycles`, the transaction is rejected: [7](#0-6) 

This means the attacker must craft a script that genuinely consumes close to `max_block_cycles` cycles — but CKB-VM scripts can trivially do this with a loop.

---

### Impact Explanation

A remote peer can relay a transaction whose script consumes up to `max_block_cycles` (3,500,000,000) cycles. The node will spend the full verification time on that transaction, regardless of the operator's `max_tx_verify_cycles` setting. With the relay rate limiter at 30 messages/second per peer, an attacker can sustain a continuous high-CPU load on the target node, degrading its ability to process legitimate transactions and potentially causing it to fall behind in block/tx processing. The per-transaction cycle cap — the primary DoS mitigation for the tx-pool — is rendered ineffective for the relay path.

---

### Likelihood Explanation

Any unprivileged peer connected to the node can send `RelayTransactions` messages. Crafting a high-cycle script (e.g., a tight loop in RISC-V) is straightforward. The relay rate limiter (30 req/s) and verify queue size cap (256 MB) provide partial mitigation but do not prevent the bypass of `max_tx_verify_cycles`. The attack requires no special privileges, no key material, and no majority hashpower.

---

### Recommendation

Enforce `max_tx_verify_cycles` as a hard rejection limit in the relay path. Before enqueuing a relayed transaction for verification, check whether `declared_cycles > max_tx_verify_cycles` and reject (and optionally ban) the peer if so. This mirrors the existing `max_block_cycles` check in `TransactionsProcess::execute()` and should be added immediately after it:

```rust
// existing check
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_block_cycles) {
    self.nc.ban_peer(...);
    return Status::ok();
}

// add: enforce per-tx limit
let max_tx_verify_cycles = self.relayer.shared().tx_pool_config().max_tx_verify_cycles;
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_tx_verify_cycles) {
    // reject or warn; do not ban (operator config, not consensus violation)
    return Status::ok();
}
```

---

### Proof of Concept

1. Configure a CKB node with `max_tx_verify_cycles = 70_000_000` (the default).
2. Craft a CKB transaction whose lock/type script contains a RISC-V loop consuming ~3,499,999,999 cycles.
3. Connect to the target node as a peer via the P2P relay protocol.
4. Send a `RelayTransactions` message with `declared_cycles = 3_499_999_999`.
5. The node's `TransactionsProcess::execute()` passes the check (`3_499_999_999 < max_block_cycles`).
6. `_process_tx` sets `max_cycles = 3_499_999_999` and runs the CKB-VM for the full duration.
7. The node spends ~50× more CPU time than `max_tx_verify_cycles` intended, per transaction.
8. Repeat at 30 tx/s (rate limit) to sustain continuous CPU saturation. [5](#0-4) [4](#0-3) [8](#0-7)

### Citations

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```

**File:** CHANGELOG.md (L1748-1750)
```markdown
- #1784: Support limit `max_tx_verify_cycles` (@jjyr)

    The purpose is to limit max verify cycles on single tx, to reduce DDOS vulnerability.
```

**File:** tx-pool/src/component/verify_queue.rs (L56-75)
```rust
pub(crate) struct VerifyQueue {
    /// inner tx entry
    inner: MultiIndexVerifyEntryMap,
    /// subscribe this notify to get be notified when there is item in the queue
    ready_rx: Arc<Notify>,
    /// total tx size in the queue, will reject new transaction if exceed the limit
    total_tx_size: usize,
    /// large cycle threshold, from `pool_config.max_tx_verify_cycles`
    large_cycle_threshold: u64,
}

impl VerifyQueue {
    /// Create a new VerifyQueue
    pub(crate) fn new(large_cycle_threshold: u64) -> Self {
        VerifyQueue {
            inner: MultiIndexVerifyEntryMap::default(),
            ready_rx: Arc::new(Notify::new()),
            total_tx_size: 0,
            large_cycle_threshold,
        }
```

**File:** tx-pool/src/process.rs (L719-732)
```rust
        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
```

**File:** tx-pool/src/process.rs (L736-748)
```rust
        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
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
            return Status::ok();
        }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L13-14)
```rust
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```
