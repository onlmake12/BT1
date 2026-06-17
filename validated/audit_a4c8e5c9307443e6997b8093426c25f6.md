### Title
Unchecked `u64 as i64` Cast Bypasses Publish-Time Range Validation in `parse_price_feed_updates_internal` — (File: `target_chains/stylus/contracts/pyth-receiver/src/lib.rs`)

---

### Summary

In the Stylus (Arbitrum) Pyth receiver, `parse_price_feed_updates_internal` compares the `i64` `publish_time` from a price message against caller-supplied `u64` bounds using unchecked `as i64` casts. When either bound exceeds `i64::MAX` (≥ 2^63), the cast wraps to a negative value, silently inverting or nullifying the time-range guard. Any unprivileged caller can exploit this to have the function accept a price whose `publish_time` lies entirely outside the requested window.

---

### Finding Description

`parse_price_feed_updates_internal` enforces a publish-time window at:

```rust
// lib.rs lines 461-467
if (min_allowed_publish_time > 0
    && publish_time < min_allowed_publish_time as i64)   // ← unchecked u64→i64
    || (max_allowed_publish_time > 0
        && publish_time > max_allowed_publish_time as i64) // ← unchecked u64→i64
{
    return Err(PythReceiverError::PriceFeedNotFoundWithinRange);
}
```

`publish_time` is `i64` (from `price_feed_message.publish_time`), while `min_allowed_publish_time` and `max_allowed_publish_time` are `u64`. Rust's `as` cast is a bit-reinterpretation: any value ≥ 2^63 becomes a negative `i64`.

**Bypass scenario (min-bound):**

| Caller input | `as i64` result | Condition evaluated | Effect |
|---|---|---|---|
| `min_allowed_publish_time = 2^63` | `i64::MIN` (-9223372036854775808) | `publish_time < i64::MIN` | Always **false** → check skipped |
| `min_allowed_publish_time = u64::MAX` | `-1` | `publish_time < -1` | False for any valid positive timestamp → check skipped |

With `min_allowed_publish_time = 2^63` and `max_allowed_publish_time = 0` (which disables the max check via the `> 0` guard), **every price message passes regardless of its actual publish time**, including arbitrarily old or future prices.

The three public entry points that flow into this function are:

- `parse_price_feed_updates` (line 324) → `parse_price_feed_updates_with_config` → `parse_price_feed_updates_internal`
- `parse_price_feed_updates_unique` (line 521) → same chain
- `parse_price_feed_updates_with_config` (line 343) → `parse_price_feed_updates_internal`

All are callable by any unprivileged transaction sender with no special role required. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

`parse_price_feed_updates` is the canonical API for consumers who need a Pyth price **at a specific historical time** (e.g., for TWAP settlement, options expiry, or any time-locked financial primitive). The function's entire value proposition is that it guarantees the returned price falls within `[minPublishTime, maxPublishTime]`. With the bypass active, the returned price can be from any point in time, breaking that guarantee.

A downstream smart contract that calls `parse_price_feed_updates` with a tight time window and then uses the returned price for settlement can be fed a stale or manipulated price that would otherwise have been rejected. This can lead to incorrect settlement values, mispriced options, or incorrect collateral valuations — all triggered by a single crafted call from an unprivileged attacker who supplies the `update_data` and the overflowed `min_publish_time`.

---

### Likelihood Explanation

The three public entry points accept `min_publish_time: u64` and `max_publish_time: u64` directly from the caller with no pre-validation. No privileged role is required. The attacker only needs to:

1. Obtain any valid signed price update (freely available from Hermes).
2. Call `parse_price_feed_updates` with `min_publish_time = 2^63` and `max_publish_time = 0`.

The Stylus receiver is a production contract deployed on Arbitrum. The attack is deterministic and requires no brute force, no key compromise, and no governance majority.

---

### Recommendation

Replace the unchecked `as i64` casts with checked conversions and reject inputs that exceed `i64::MAX`:

```rust
// Validate bounds before use
let min_publish_time_i64 = i64::try_from(min_allowed_publish_time)
    .map_err(|_| PythReceiverError::InvalidUpdateData)?;
let max_publish_time_i64 = i64::try_from(max_allowed_publish_time)
    .map_err(|_| PythReceiverError::InvalidUpdateData)?;

if (min_allowed_publish_time > 0 && publish_time < min_publish_time_i64)
    || (max_allowed_publish_time > 0 && publish_time > max_publish_time_i64)
{
    return Err(PythReceiverError::PriceFeedNotFoundWithinRange);
}
```

Alternatively, widen the comparison domain by casting `publish_time` to `i128` and the bounds to `i128` (via `u64 as i128`, which is always lossless), then compare in `i128` space.

---

### Proof of Concept

```
Attacker calls (on Arbitrum Stylus receiver):
  parse_price_feed_updates(
      update_data  = <valid Wormhole-signed message with publish_time = 1_000_000>,  // year 2001 price
      price_ids    = [<target feed id>],
      min_publish_time = 9_223_372_036_854_775_808,  // 2^63, wraps to i64::MIN = -9223372036854775808
      max_publish_time = 0,                           // disables max check (> 0 guard is false)
  )

Inside parse_price_feed_updates_internal:
  publish_time              = 1_000_000  (i64, from message)
  min_allowed_publish_time  = 9_223_372_036_854_775_808  (u64)
  min_allowed_publish_time as i64 = -9_223_372_036_854_775_808  (i64::MIN)

  Check: publish_time < i64::MIN
       = 1_000_000 < -9_223_372_036_854_775_808
       = false  → guard NOT triggered

  max check: max_allowed_publish_time > 0 → 0 > 0 → false → guard NOT triggered

Result: price with publish_time = 1_000_000 (year 2001) is returned as if it
        satisfies the caller's time window, bypassing the range guarantee entirely.
``` [4](#0-3)

### Citations

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L324-341)
```rust
    pub fn parse_price_feed_updates(
        &mut self,
        update_data: Vec<u8>,
        price_ids: Vec<[u8; 32]>,
        min_publish_time: u64,
        max_publish_time: u64,
    ) -> Result<Vec<PriceFeedReturn>, PythReceiverError> {
        let price_feeds = self.parse_price_feed_updates_with_config(
            vec![update_data],
            price_ids,
            min_publish_time,
            max_publish_time,
            false,
            false,
            false,
        );
        price_feeds
    }
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L458-500)
```rust
                        Message::PriceFeedMessage(price_feed_message) => {
                            let publish_time = price_feed_message.publish_time;

                            if (min_allowed_publish_time > 0
                                && publish_time < min_allowed_publish_time as i64)
                                || (max_allowed_publish_time > 0
                                    && publish_time > max_allowed_publish_time as i64)
                            {
                                return Err(PythReceiverError::PriceFeedNotFoundWithinRange);
                            }

                            let price_id_fb = FixedBytes::<32>::from(price_feed_message.feed_id);

                            if check_uniqueness {
                                let prev_price_info = self.latest_price_info.get(price_id_fb);
                                let prev_publish_time =
                                    prev_price_info.publish_time.get().to::<u64>();

                                if prev_publish_time > 0
                                    && min_allowed_publish_time <= prev_publish_time
                                {
                                    return Err(PythReceiverError::PriceFeedNotFoundWithinRange);
                                }
                            }

                            let expo = I32::try_from(price_feed_message.exponent)
                                .map_err(|_| PythReceiverError::InvalidUpdateData)?;
                            let price = I64::try_from(price_feed_message.price)
                                .map_err(|_| PythReceiverError::InvalidUpdateData)?;
                            let ema_price = I64::try_from(price_feed_message.ema_price)
                                .map_err(|_| PythReceiverError::InvalidUpdateData)?;

                            let price_info_return = (
                                price_id_fb,
                                U64::from(publish_time),
                                expo,
                                price,
                                U64::from(price_feed_message.conf),
                                ema_price,
                                U64::from(price_feed_message.ema_conf),
                            );

                            price_feeds.push(price_info_return);
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L521-538)
```rust
    pub fn parse_price_feed_updates_unique(
        &mut self,
        update_data: Vec<Vec<u8>>,
        price_ids: Vec<[u8; 32]>,
        min_publish_time: u64,
        max_publish_time: u64,
    ) -> Result<Vec<PriceFeedReturn>, PythReceiverError> {
        let price_feeds = self.parse_price_feed_updates_with_config(
            update_data,
            price_ids,
            min_publish_time,
            max_publish_time,
            true,
            false,
            false,
        );
        price_feeds
    }
```
