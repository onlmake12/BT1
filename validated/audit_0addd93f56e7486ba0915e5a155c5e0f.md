### Title
`ema_price_no_older_than` Missing Saturating Subtraction Causes u64 Underflow DoS on Fuel — (`target_chains/fuel/contracts/pyth-contract/src/main.sw`)

---

### Summary

The private `ema_price_no_older_than` function in the Fuel Pyth contract performs an unchecked `u64` subtraction `current_time - price.publish_time`. In Sway (FuelVM), unsigned integer underflow is a hard panic. The sibling function `price_no_older_than` in the same file explicitly guards against this with a saturating subtraction, but `ema_price_no_older_than` does not. Any unprivileged updater who submits a valid price update whose `publish_time` is even one TAI64 unit ahead of the current Fuel block timestamp will permanently brick all callers of `ema_price()` and `ema_price_no_older_than()` until a subsequent update with a non-future timestamp is accepted.

---

### Finding Description

In `target_chains/fuel/contracts/pyth-contract/src/main.sw`, two staleness-check helpers exist side by side:

**`price_no_older_than` (safe — lines 331–343):**
```sway
fn price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = price_unsafe(price_feed_id);
    let current_time = timestamp();
    // Mimicking saturating subtraction to avoid underflow
    let time_difference = if current_time > price.publish_time {
        current_time - price.publish_time
    } else {
        0
    };
    require(time_difference <= time_period, PythError::OutdatedPrice);
    price
}
```

**`ema_price_no_older_than` (vulnerable — lines 311–320):**
```sway
fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = ema_price_unsafe(price_feed_id);
    let current_time = timestamp();
    require(
        current_time - price.publish_time <= time_period,  // ← no guard
        PythError::OutdatedPrice,
    );
    price
}
```

The comment in `price_no_older_than` explicitly acknowledges the underflow risk and applies a saturating guard. `ema_price_no_older_than` omits this guard entirely.

`Price.publish_time` is typed as `u64` in TAI64 format: [1](#0-0) 

`timestamp()` from `std::block::timestamp` also returns TAI64 `u64`. In Sway, `u64` subtraction that would produce a negative result is a hard VM panic (not a graceful revert with an error code). When `price.publish_time > current_time`, the expression `current_time - price.publish_time` panics unconditionally.

The public entry point `ema_price()` delegates directly to this vulnerable helper: [2](#0-1) 

The vulnerable helper itself: [3](#0-2) 

The safe sibling for comparison: [4](#0-3) 

---

### Impact Explanation

Any DeFi protocol on Fuel that calls `ema_price()` or `ema_price_no_older_than()` will receive a hard VM panic instead of a price. This is a **complete DoS** of the EMA price feed interface on Fuel for the duration between the offending update and the next valid (non-future-timestamped) update being accepted. Protocols that use EMA prices for liquidation guards, collateral valuation, or circuit breakers will be unable to execute those operations, potentially trapping user funds or preventing time-sensitive actions.

---

### Likelihood Explanation

Pyth price updates originate from Pythnet (a Solana-based app-chain) and are relayed via Wormhole. The `publish_time` embedded in the signed VAA payload reflects Pythnet's clock. Fuel's block timestamps are independent. A clock skew of even 1 second between Pythnet validators and the Fuel sequencer is sufficient to produce a `publish_time` that is ahead of `current_time` on Fuel. This is a realistic, non-adversarial condition that occurs in normal operation. An adversarial updater can also deliberately select a VAA with a future-skewed timestamp from Hermes and submit it via the permissionless `update_price_feeds` entry point. [5](#0-4) 

---

### Recommendation

Apply the same saturating subtraction guard already present in `price_no_older_than`:

```sway
fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = ema_price_unsafe(price_feed_id);
    let current_time = timestamp();
    // Mimicking saturating subtraction to avoid underflow
    let time_difference = if current_time > price.publish_time {
        current_time - price.publish_time
    } else {
        0
    };
    require(time_difference <= time_period, PythError::OutdatedPrice);
    price
}
```

This matches the fix pattern already applied to `price_no_older_than` and mirrors the `diff()` helper used in the EVM `AbstractPyth.sol`: [6](#0-5) 

---

### Proof of Concept

1. Obtain a valid signed VAA from Hermes for any price feed where the embedded `publish_time` (TAI64) is ≥ 1 unit ahead of the current Fuel block `timestamp()`.
2. Call `update_price_feeds(update_data)` on the Fuel Pyth contract. The update is accepted because the price-update path does not check for future timestamps.
3. The stored `ema_price.publish_time` is now `> current_time`.
4. Any subsequent call to `ema_price(price_feed_id)` or `ema_price_no_older_than(time_period, price_feed_id)` executes `current_time - price.publish_time` where `price.publish_time > current_time`, triggering a Sway u64 underflow panic.
5. All Fuel DeFi consumers of EMA prices are bricked until a new update with `publish_time ≤ current_time` is stored.

### Citations

**File:** target_chains/fuel/contracts/pyth-interface/src/data_structures/price.sw (L29-31)
```text
    // The TAI64 timestamp describing when the price was published
    pub publish_time: u64,
}
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L124-127)
```text
    #[storage(read)]
    fn ema_price(price_feed_id: PriceFeedId) -> Price {
        ema_price_no_older_than(valid_time_period(), price_feed_id)
    }
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L311-320)
```text
fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = ema_price_unsafe(price_feed_id);
    let current_time = timestamp();
    require(
        current_time - price.publish_time <= time_period,
        PythError::OutdatedPrice,
    );

    price
}
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L330-343)
```text
#[storage(read)]
fn price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = price_unsafe(price_feed_id);
    let current_time = timestamp();
    // Mimicking saturating subtraction to avoid underflow
    let time_difference = if current_time > price.publish_time {
        current_time - price.publish_time
    } else {
        0
    };
    require(time_difference <= time_period, PythError::OutdatedPrice);

    price
}
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L377-420)
```text
#[storage(read, write)]
fn update_price_feeds(update_data: Vec<Bytes>) {
    require(
        msg_asset_id() == AssetId::base(),
        PythError::FeesCanOnlyBePaidInTheBaseAsset,
    );

    let mut total_number_of_updates = 0;

    // let mut updated_price_feeds: Vec<PriceFeedId> = Vec::new(); // TODO: requires append for Vec
    let mut i = 0;
    while i < update_data.len() {
        let data = update_data.get(i).unwrap();

        match UpdateType::determine_type(data) {
            UpdateType::Accumulator(accumulator_update) => {
                let (number_of_updates, _updated_ids) = accumulator_update.update_price_feeds(
                    current_guardian_set_index(),
                    storage
                        .wormhole_guardian_sets,
                    storage
                        .latest_price_feed,
                    storage
                        .is_valid_data_source,
                );
                // updated_price_feeds.append(updated_ids); // TODO: requires append for Vec
                total_number_of_updates += number_of_updates;
            },
            UpdateType::BatchAttestation(batch_attestation_update) => {
                let _updated_ids = batch_attestation_update.update_price_feeds(
                    current_guardian_set_index(),
                    storage
                        .wormhole_guardian_sets,
                    storage
                        .latest_price_feed,
                    storage
                        .is_valid_data_source,
                );
                // updated_price_feeds.append(updated_ids); // TODO: requires append for Vec
                total_number_of_updates += 1;
            },
        }

        i += 1;
```

**File:** target_chains/ethereum/sdk/solidity/AbstractPyth.sol (L81-87)
```text
    function diff(uint x, uint y) internal pure returns (uint) {
        if (x > y) {
            return x - y;
        } else {
            return y - x;
        }
    }
```
