### Title
Unsigned Integer Underflow in `ema_price_no_older_than` Causes Unexpected Revert When `publish_time` Exceeds Block Timestamp — (File: `target_chains/fuel/contracts/pyth-contract/src/main.sw`)

---

### Summary

The private `ema_price_no_older_than` function in the Fuel Pyth contract performs an unchecked unsigned integer subtraction `current_time - price.publish_time`. In Sway, subtracting a larger `u64` from a smaller `u64` causes a runtime panic/revert. If the stored EMA price's `publish_time` is even one second ahead of the Fuel block's `timestamp()`, both the public `ema_price` and `ema_price_no_older_than` entry points revert with a panic instead of the expected `PythError::OutdatedPrice`. The sibling function `price_no_older_than` already contains an explicit fix for this exact issue, but the fix was never applied to the EMA variant.

---

### Finding Description

In `target_chains/fuel/contracts/pyth-contract/src/main.sw`, the private helper `ema_price_no_older_than` (lines 311–320) computes the age of the stored EMA price as:

```sway
// lines 314–317
require(
    current_time - price.publish_time <= time_period,
    PythError::OutdatedPrice,
);
```

`current_time` and `price.publish_time` are both `u64`. In Sway, unsigned integer subtraction that would produce a negative result causes an integer underflow panic, reverting the entire transaction. There is no guard checking `current_time >= price.publish_time` before the subtraction.

The sibling function `price_no_older_than` (lines 331–343) contains an explicit comment and guard for exactly this scenario:

```sway
// Mimicking saturating subtraction to avoid underflow
let time_difference = if current_time > price.publish_time {
    current_time - price.publish_time
} else {
    0
};
```

This guard is entirely absent from `ema_price_no_older_than`.

Both public ABI entry points `ema_price` (line 125–127) and `ema_price_no_older_than` (line 130–132) delegate directly to this vulnerable private function:

```sway
fn ema_price(price_feed_id: PriceFeedId) -> Price {
    ema_price_no_older_than(valid_time_period(), price_feed_id)
}

fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    ema_price_no_older_than(time_period, price_feed_id)
}
```

**How `publish_time > current_time` occurs:** Pyth price updates are signed by Wormhole guardians and carry a `publish_time` sourced from Pythnet's clock. All other Pyth chain implementations explicitly account for the case where `publish_time` slightly exceeds the local block timestamp due to clock drift between Pythnet and the target chain:

- **Ethereum** (`AbstractPyth.sol` line 81–87): uses `diff(block.timestamp, price.publishTime)` — absolute difference, handles both directions.
- **Starknet** (`pyth.cairo` line 757–765): uses `if current >= publish_time { current - publish_time } else { 0 }`.
- **Stylus** (`lib.rs` line 547–551): uses `saturating_sub`.
- **TON** (`Pyth.fc` line 137): uses `max(0, current_time - publish_time)`.
- **Fuel `price_no_older_than`** (lines 334–339): uses the same saturating pattern.

Only `ema_price_no_older_than` in Fuel is missing this protection.

An unprivileged updater can submit a valid, guardian-signed price update with a `publish_time` that is a few seconds in the future relative to the Fuel block timestamp (a normal occurrence given Pythnet-to-Fuel clock drift). Once this update is stored, every subsequent call to `ema_price` or `ema_price_no_older_than` panics until the Fuel block timestamp catches up.

---

### Impact Explanation

Any DeFi protocol on Fuel that calls `ema_price` or `ema_price_no_older_than` to read EMA prices from Pyth will receive an unexpected revert (panic) rather than a graceful `OutdatedPrice` error. This can:

- Brick lending protocols, AMMs, or derivatives platforms that depend on EMA prices for liquidations, collateral checks, or settlement.
- Cause cascading failures in any contract that does not expect a panic from a Pyth price read.

The window of impact lasts until the Fuel block timestamp surpasses the stored `publish_time`, which could be seconds to minutes depending on clock drift.

---

### Likelihood Explanation

Clock drift between Pythnet and the Fuel network is a realistic and expected condition — it is the explicit reason every other Pyth chain implementation uses absolute difference or saturating subtraction. Any unprivileged user who submits a valid Wormhole-signed price update (the normal `update_price_feeds` flow) with a `publish_time` slightly ahead of the current Fuel block timestamp triggers this bug. No special privileges or key compromise are required.

---

### Recommendation

Apply the same saturating subtraction guard already present in `price_no_older_than` to `ema_price_no_older_than`:

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

---

### Proof of Concept

1. An unprivileged updater calls `update_price_feeds` with a valid Wormhole-signed accumulator update whose EMA price `publish_time` is `T + 5` (5 seconds ahead of the current Fuel block timestamp `T`). This is accepted because `update_price_feeds` does not validate `publish_time` against the current block time.

2. The stored `latest_price_feed[id].ema_price.publish_time` is now `T + 5`.

3. Any caller (e.g., a lending protocol) calls `ema_price(id)` or `ema_price_no_older_than(60, id)`.

4. Inside `ema_price_no_older_than`:
   - `current_time = timestamp()` → returns `T` (or `T + 1`, still less than `T + 5`).
   - `current_time - price.publish_time` → `T - (T + 5)` → unsigned underflow → **Sway runtime panic, transaction reverts**.

5. The caller receives a panic revert instead of `PythError::OutdatedPrice`. Any protocol that catches `OutdatedPrice` to handle stale prices will not catch this panic, causing unexpected failures.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L123-132)
```text
impl PythCore for Contract {
    #[storage(read)]
    fn ema_price(price_feed_id: PriceFeedId) -> Price {
        ema_price_no_older_than(valid_time_period(), price_feed_id)
    }

    #[storage(read)]
    fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
        ema_price_no_older_than(time_period, price_feed_id)
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
