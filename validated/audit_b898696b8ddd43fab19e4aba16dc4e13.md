### Title
Underflow in `ema_price_no_older_than` Causes Unnecessary Reverts When Pythnet Timestamps Are Ahead of Fuel Chain — (File: `target_chains/fuel/contracts/pyth-contract/src/main.sw`)

---

### Summary

The `ema_price_no_older_than` function in the Fuel Pyth contract performs a raw u64 subtraction `current_time - price.publish_time` without underflow protection. The sibling function `price_no_older_than` in the same file explicitly guards against this with a saturating subtraction pattern. When a Pythnet `publish_time` is even slightly ahead of the Fuel chain's `timestamp()`, the subtraction underflows, causing a panic/revert. This blocks any user from reading EMA prices via the safe staleness-checked API, even when the price is perfectly fresh.

---

### Finding Description

In `target_chains/fuel/contracts/pyth-contract/src/main.sw`, two private staleness-check functions exist side by side:

**`price_no_older_than` (lines 331–343) — has underflow protection:**
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

**`ema_price_no_older_than` (lines 311–320) — missing underflow protection:**
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
```

Sway u64 arithmetic panics on underflow. When `price.publish_time > current_time` — a documented and expected condition since Pythnet timestamps can be slightly ahead of target chain timestamps — the expression `current_time - price.publish_time` underflows and the transaction reverts.

The Scheduler contract in the EVM implementation explicitly acknowledges this phenomenon with the comment: *"Use distance (absolute difference) since pythnet timestamps may be slightly ahead of this chain."* The EVM `AbstractPyth.sol` and the Aptos/Sui/Near implementations all use absolute-difference (`abs_diff` / `diff`) for exactly this reason. The Fuel `price_no_older_than` function was also fixed with a saturating subtraction guard. The `ema_price_no_older_than` function was not. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

---

### Impact Explanation

Any user or contract calling `ema_price_no_older_than` on the Fuel Pyth contract when the stored EMA price's `publish_time` is even 1 second ahead of the Fuel chain's `timestamp()` will receive a panic/revert instead of the price. This is a denial-of-service on EMA price reads via the safe API. Protocols relying on EMA prices for liquidations, collateral valuation, or other time-sensitive operations will be blocked from functioning during these windows.

**Impact: Medium** — EMA price reads are blocked; the unsafe `ema_price_unsafe` fallback exists but bypasses staleness protection entirely.

---

### Likelihood Explanation

Pythnet timestamps being slightly ahead of target chain timestamps is a known, recurring condition explicitly documented and mitigated in every other Pyth chain implementation. The Fuel `price_no_older_than` function was already patched for this exact reason. The EMA variant was missed. This condition occurs regularly in normal operation, not just under adversarial conditions.

**Likelihood: High** — Normal Pythnet operation produces timestamps that can be slightly ahead of any target chain.

---

### Recommendation

Apply the same saturating subtraction guard used in `price_no_older_than` to `ema_price_no_older_than`:

```sway
fn ema_price_no_older_than(time_period: u64, price_feed_id: PriceFeedId) -> Price {
    let price = ema_price_unsafe(price_feed_id);
    let current_time = timestamp();
    let time_difference = if current_time > price.publish_time {
        current_time - price.publish_time
    } else {
        0
    };
    require(time_difference <= time_period, PythError::OutdatedPrice);
    price
}
```

This mirrors the fix already applied to `price_no_older_than` and matches the `abs_diff`/`distance` pattern used in all other Pyth chain implementations.

---

### Proof of Concept

1. Pythnet publishes an EMA price update with `publish_time = T`.
2. The Fuel chain's `timestamp()` returns `T - 1` (one second behind Pythnet, a normal condition).
3. A user calls the public `ema_price_no_older_than(60, price_feed_id)` ABI function.
4. Internally, `current_time = T - 1` and `price.publish_time = T`.
5. The expression `current_time - price.publish_time = (T-1) - T` underflows u64, causing a Sway panic/revert.
6. The user cannot read the EMA price despite it being fresh (only 1 second old, well within the 60-second window).
7. The same call to `price_no_older_than(60, price_feed_id)` succeeds because it uses the saturating subtraction guard. [1](#0-0) [6](#0-5)

### Citations

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

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L331-343)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L549-552)
```text
        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();
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

**File:** target_chains/aptos/contracts/sources/pyth.move (L483-486)
```text
    fun check_price_is_fresh(price: &Price, max_age_secs: u64) {
        let age = abs_diff(timestamp::now_seconds(), price::get_timestamp(price));
        assert!(age < max_age_secs, error::stale_price_update());
    }
```
