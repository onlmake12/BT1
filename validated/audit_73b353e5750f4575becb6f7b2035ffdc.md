The discrepancy is real and confirmed by the code. Here is the full trace:

**Step 1 — `check_tx_fee` uses raw `tx_size`, not weight** [1](#0-0) 

The comment at line 42 explicitly acknowledges this: `"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"`. The minimum fee is computed as `min_fee_rate.fee(tx_size)`, ignoring cycles entirely.

**Step 2 — `TxEntry` is created with actual verified cycles after script execution** [2](#0-1) 

After `verify_rtx` returns `verified.cycles`, the entry is created with the real cycle count. No second fee check against weight is performed.

**Step 3 — Block assembly ordering uses `get_transaction_weight(size, cycles)`** [3](#0-2) 

`AncestorsScoreSortKey` is built from `get_transaction_weight(entry.size, entry.cycles)` = `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. [4](#0-3) 

**Step 4 — Numeric confirmation**

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`:
- Attacker tx: `size=200`, `cycles=70_000_000`
- `check_tx_fee` min_fee: `1000 * 200 / 1000 = 200 shannons` → passes with `fee=200`
- Actual weight: `max(200, 70_000_000 × 0.000_170_571_4) = 11,940`
- Effective fee_rate in pool: `200 × 1000 / 11940 ≈ 16.75 shannons/KW` — 60× below `min_fee_rate=1000`

**Step 5 — No compensating post-verification check**

The full `_process_tx` flow is: `pre_check` → `verify_rtx` → `TxEntry::new` → `submit_entry`. There is no second call to `check_tx_fee` or any equivalent weight-based gate after cycles are known. [5](#0-4) 

---

### Title
`check_tx_fee` admits transactions whose weight-based fee_rate is far below `min_fee_rate`, allowing sub-minimum-fee-rate transactions to enter the pool and be included in blocks — (`tx-pool/src/util.rs`)

### Summary
`check_tx_fee` enforces `min_fee_rate` against raw serialized `tx_size`, but the pool's ordering and block assembly use `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An attacker can craft a transaction with small size but high cycle consumption that passes the fee gate yet has an effective weight-based fee_rate orders of magnitude below `min_fee_rate`.

### Finding Description
In `tx-pool/src/util.rs`, `check_tx_fee` computes `min_fee = min_fee_rate.fee(tx_size)` using only the serialized byte size. The code itself comments: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check."* After script verification in `_process_tx`, the actual cycle count is known and stored in `TxEntry`. All subsequent pool operations — `AncestorsScoreSortKey`, `EvictKey`, `fee_rate()` — use `get_transaction_weight(size, cycles)`. There is no second fee check using the true weight after cycles are determined.

### Impact Explanation
Transactions with sub-minimum weight-based fee_rates enter the pool and can be included in mined blocks. A miner assembling a block template selects transactions by weight-based fee_rate; a high-cycle/low-size transaction occupies disproportionate block cycle budget relative to the fee paid, reducing block revenue. An attacker can flood the pool with such transactions, each paying only `min_fee_rate × tx_size` shannons while consuming `~60×` more effective block weight.

### Likelihood Explanation
The attack requires only an unprivileged RPC `send_transaction` call. The attacker controls script content and can tune cycles to any desired value up to `max_block_cycles`. The discrepancy is structural and reproducible on any default-configured node.

### Recommendation
After `verify_rtx` returns the actual cycle count, perform a second fee check using the true weight:
```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```
This closes the gap between admission policy and block-assembly ordering.

### Proof of Concept
```
tx_size = 200 bytes
cycles  = 70_000_000
fee     = 200 shannons  (= min_fee_rate × tx_size / 1000)

check_tx_fee:  min_fee = 1000 × 200 / 1000 = 200  → PASS

weight = max(200, 70_000_000 × 0.000_170_571_4) = 11_940
effective_fee_rate = 200 × 1000 / 11_940 ≈ 16.75 shannons/KW

invariant violated: 16.75 << min_fee_rate (1000)
```
Fuzz any `(tx_size, cycles)` pair where `fee = min_fee_rate.fee(tx_size)` and assert `FeeRate::calculate(fee, get_transaction_weight(tx_size, cycles)) >= min_fee_rate`. The assertion fails for all pairs where `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L715-754)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

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

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

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
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```
