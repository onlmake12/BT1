Audit Report

## Title
Unconditional `expect()` on `txs_sizes` in `FeeRateCollector::statistics` panics on pre-v0.106 `BlockExt` records — (`rpc/src/util/fee_rate.rs`)

## Summary
The `From<packed::BlockExtReader>` conversion for old 5-field `BlockExt` records hard-codes `txs_sizes: None`. The `collect()` closure in `FeeRateCollector::statistics` calls `.expect()` on `txs_sizes` unconditionally, with no guard for the `None` case. Any node that upgraded in-place from before v0.106 retains old-format records in `COLUMN_BLOCK_EXT`, and a single unauthenticated RPC call to `get_fee_rate_statistics` will trigger the panic, crashing the RPC handler task.

## Finding Description
**Deserialization yields `txs_sizes: None` for old records.**
Both `Unpack<core::BlockExt> for packed::BlockExtReader` (lines 139–151) and `From<packed::BlockExtReader> for core::BlockExt` (lines 154–165) in `util/types/src/conversion/storage.rs` hard-code `txs_sizes: None`. [1](#0-0) 

**`get_block_ext` dispatches old records to this path.**
In `store/src/store.rs`, `count_extra_fields() == 0` routes to `reader.into()`, which invokes the old `BlockExtReader` conversion and produces `txs_sizes: None`. [2](#0-1) 

**`filter_map` does not filter `txs_sizes: None`.**
`collect()` uses `filter_map` only to drop blocks where `get_block_ext_by_number` returns `None` (i.e., missing block hash). Old-format blocks return `Some(BlockExt { txs_sizes: None, … })` and are passed directly to the closure. [3](#0-2) 

**Unconditional `expect` fires on every old-format block.**
Line 93 of `rpc/src/util/fee_rate.rs` calls `txs_sizes.expect("expect txs_size's length >= 1")` with no `if let Some` guard, no early return, and no `filter` step. [4](#0-3) 

**No `catch_unwind` in the RPC layer.**
A search of `rpc/**/*.rs` finds zero uses of `catch_unwind` or equivalent panic barriers, so the panic propagates unguarded through the synchronous handler. [5](#0-4) 

**Test suite never exercises `txs_sizes: None`.**
Every entry in `rpc/src/tests/fee_rate.rs` sets `txs_sizes: Some(…)`, so this code path has no existing test coverage. [6](#0-5) 

## Impact Explanation
The panic aborts the RPC handler task for `get_fee_rate_statistics` (and its deprecated alias `get_fee_rate_statics`). In tokio's task model, a panic in a spawned task is caught by the runtime, causing the task to abort and return a `JoinError`; the node process itself continues running. The concrete, reproducible impact is an RPC API crash on every call to this endpoint while old-format records remain in the database. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash.** The higher claim of full node-process termination is not concretely proven by the submitted evidence, and severity is not upgraded beyond what the evidence supports.

## Likelihood Explanation
Any mainnet or testnet node that was running before v0.106 and upgraded in-place (without a full re-sync) retains old-format `BlockExt` records for every block written before the upgrade. No authentication, key, peer relationship, or PoW is required. The attacker only needs the node's public JSON-RPC endpoint. The call is repeatable and deterministic: every invocation of `get_fee_rate_statistics` while old records are in the window will panic.

## Recommendation
Replace the unconditional `expect` at `rpc/src/util/fee_rate.rs` line 93 with a graceful skip:

```rust
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip pre-v0.106 blocks silently
};
```

Alternatively, filter in `collect()` by adding `.filter(|ext| ext.txs_sizes.is_some())` before `.fold(…)`, or add a database migration that back-fills `txs_sizes` for old blocks during node startup.

## Proof of Concept
1. Start a CKB node that has `COLUMN_BLOCK_EXT` entries with 5-field `BlockExt` (any node upgraded from before v0.106 without re-sync).
2. Send:
   ```json
   {"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":[{"value":"0x65"}],"id":1}
   ```
3. `collect(101, …)` iterates the last 101 blocks, hits the first old-format block, deserializes it to `core::BlockExt { txs_sizes: None, … }`, and the closure fires `None.expect("expect txs_size's length >= 1")` → panic → RPC task abort.

A minimal unit test reproduces this without a real database: insert a `BlockExt { txs_sizes: None, … }` into `DummyFeeRateProvider` and call `FeeRateCollector::new(&provider).statistics(None)` — the existing test harness in `rpc/src/tests/fee_rate.rs` already provides all the scaffolding needed. [7](#0-6)

### Citations

**File:** util/types/src/conversion/storage.rs (L154-165)
```rust
impl<'r> From<packed::BlockExtReader<'r>> for core::BlockExt {
    fn from(value: packed::BlockExtReader<'r>) -> core::BlockExt {
        core::BlockExt {
            received_at: value.received_at().into(),
            total_difficulty: value.total_difficulty().into(),
            total_uncles_count: value.total_uncles_count().into(),
            verified: value.verified().into(),
            txs_fees: value.txs_fees().into(),
            cycles: None,
            txs_sizes: None,
        }
    }
```

**File:** store/src/store.rs (L252-261)
```rust
                match reader.count_extra_fields() {
                    0 => reader.into(),
                    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
                    _ => {
                        panic!(
                            "BlockExt storage field count doesn't match, expect 7 or 5, actual {}",
                            reader.field_count()
                        )
                    }
                }
```

**File:** rpc/src/util/fee_rate.rs (L45-47)
```rust
        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L79-121)
```rust
    pub fn statistics(&self, target: Option<u64>) -> Option<FeeRateStatistics> {
        let mut target = target.unwrap_or(DEFAULT_TARGET);
        if is_even(target) {
            target = target.saturating_add(1);
        }
        target = std::cmp::min(self.provider.max_target(), target);

        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
        });

        if fee_rates.is_empty() {
            None
        } else {
            Some(FeeRateStatistics {
                mean: mean(&fee_rates).into(),
                median: median(&mut fee_rates).into(),
            })
        }
    }
```

**File:** rpc/src/tests/fee_rate.rs (L6-45)
```rust
struct DummyFeeRateProvider {
    tip_number: BlockNumber,
    block_exts: HashMap<BlockNumber, BlockExt>,
    max_target: u64,
}

impl DummyFeeRateProvider {
    pub fn new(max_target: u64) -> DummyFeeRateProvider {
        DummyFeeRateProvider {
            tip_number: 0,
            block_exts: HashMap::new(),
            max_target,
        }
    }

    pub fn append(&mut self, number: BlockNumber, ext: BlockExt) {
        if number > self.tip_number {
            self.tip_number = number;
        }
        self.block_exts.insert(number, ext);
    }

    pub fn set_max_target(&mut self, max_target: u64) {
        self.max_target = max_target
    }
}

impl FeeRateProvider for DummyFeeRateProvider {
    fn get_tip_number(&self) -> BlockNumber {
        self.tip_number
    }

    fn get_block_ext_by_number(&self, number: BlockNumber) -> Option<BlockExt> {
        self.block_exts.get(&number).cloned()
    }

    fn max_target(&self) -> u64 {
        self.max_target
    }
}
```

**File:** rpc/src/tests/fee_rate.rs (L51-65)
```rust
        let ext = BlockExt {
            received_at: 0,
            total_difficulty: 0u64.into(),
            total_uncles_count: 0,
            verified: None,

            // txs_fees length is equal to block_ext.cycles length
            // and txs_fees does not include cellbase
            txs_fees: vec![Capacity::shannons(i * i * 100)],
            // cycles does not include cellbase
            cycles: Some(vec![i * 100]),
            // txs_sizes length is equal to block_ext.txs_fees length + 1
            // first element in txs_sizes is belong to cellbase
            txs_sizes: Some(vec![i * 5678, i * 100]),
        };
```
