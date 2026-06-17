### Title
`ema_price_no_older_than()` Panics on Underflow When `publish_time > current_time` — (`File: target_chains/fuel/contracts/pyth-contract/src/main.sw`)

---

### Summary

The Fuel Pyth contract's `ema_price_no_older_than` function performs an unchecked unsigned integer subtraction `current_time - price.publish_time`. When a price feed carries a `publish_time` in the future (e.g., due to Pythnet/Fuel clock skew), this subtraction underflows and causes a Sway runtime panic, reverting the call. The sibling function `price_no_older_than` was explicitly patched with a saturating-subtraction guard for exactly this reason, but `ema_price_no_older_than` was not.

---

### Finding Description

In `target_chains/fuel/contracts/pyth-contract/src/main.sw`, the private function `ema_price_no_older_than` computes:

```sway
fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = ema_price_unsafe(price_feed_id);
    let current_time = timestamp();
    require(
        current_time - price.publish_time <= time_period,
        PythError::OutdatedPrice,
    );
    price
}
``` [1](#0-0) 

The subtraction `current_time - price.publish_time` is a bare `u64` subtraction. In Sway, unsigned integer underflow causes a runtime revert (panic). There is no guard checking `current_time >= price.publish_time` before the subtraction.

The sibling function `price_no_older_than` in the same file was explicitly fixed for this exact issue, as evidenced by the developer comment:

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
``` [2](#0-1) 

The fix was applied to `price_no_older_than` but not to `ema_price_no_older_than`. This is a direct analog to the TwabRewards `cancelPromotion()` bug: a function that should return a clean error instead panics due to an unguarded timestamp subtraction.

---

### Impact Explanation

Any on-chain consumer calling `ema_price_no_older_than` (exposed as the public `ema_price_no_older_than` ABI entry point) when the stored EMA price feed has `publish_time > current_time` will receive a hard revert instead of the expected `PythError::OutdatedPrice` error. This is a **denial-of-service** on the EMA price query path for any affected price feed. Downstream contracts that rely on EMA prices (e.g., for TWAP-based liquidations or risk calculations) will be bricked for the duration that the future-timestamped price is stored.

---

### Likelihood Explanation

Pythnet validators and Fuel block producers run independent clocks. A modest clock skew (even a few seconds) between Pythnet and the Fuel chain is realistic and has been observed in practice across other EVM-compatible chains. A price attestation signed at Pythnet time `T` can arrive and be stored on Fuel when the Fuel block timestamp is still `T - δ`. This is not a malicious act — it is a normal operational condition. The `price_no_older_than` guard comment confirms the Pyth team already acknowledged this scenario as a real risk.

---

### Recommendation

Apply the same saturating-subtraction guard used in `price_no_older_than` to `ema_price_no_older_than`:

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

1. A Pythnet price attestation is signed at time `T = 1_000_010`.
2. The attestation is relayed and `update_price_feeds` is called on Fuel when the Fuel block timestamp is `T' = 1_000_005` (5-second clock skew). The stored `ema_price.publish_time = 1_000_010`.
3. A consumer contract calls `ema_price_no_older_than(60, price_feed_id)`.
4. Inside the function: `current_time = 1_000_005`, `price.publish_time = 1_000_010`.
5. `current_time - price.publish_time` = `1_000_005 - 1_000_010` → **u64 underflow → Sway runtime panic → transaction reverts**.
6. The consumer receives a hard revert instead of `PythError::OutdatedPrice`, breaking any contract that catches that specific error or that expects a clean failure path.

The identical call to `price_no_older_than` under the same conditions would return `time_difference = 0`, pass the `<= time_period` check, and return the price successfully — demonstrating the asymmetric treatment between the two functions. [1](#0-0) [3](#0-2)

### Citations

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L311-319)
```text
fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = ema_price_unsafe(price_feed_id);
    let current_time = timestamp();
    require(
        current_time - price.publish_time <= time_period,
        PythError::OutdatedPrice,
    );

    price
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
