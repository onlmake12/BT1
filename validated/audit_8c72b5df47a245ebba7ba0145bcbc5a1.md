### Title
Incorrect Variable Used in `parsePriceFeedUpdatesUnique` Uniqueness Check — (`File: target_chains/stylus/contracts/pyth-receiver/src/lib.rs`)

### Summary

The Stylus (Arbitrum) Pyth receiver implements the `parsePriceFeedUpdatesUnique` uniqueness check using the **on-chain stored price's publish time** instead of the **`prevPublishTime` field embedded in the price update message**. This is an exact analog of the `divARITH` bug: the code performs a check against the wrong variable, making the uniqueness guarantee weaker than intended and bypassable by any transaction sender.

### Finding Description

The EVM `Pyth.sol` implementation enforces uniqueness in `_parseSingleMerkleUpdate` by checking the `prevPublishTime` field that is embedded in the Merkle-proven price update message itself:

```solidity
(!context.checkUniqueness ||
    context.minAllowedPublishTime > prevPublishTime)
``` [1](#0-0) 

Here `prevPublishTime` is extracted directly from the authenticated accumulator message via `extractPriceInfoFromMerkleProof`, making it Wormhole-attested data. The check guarantees: *"the previous Pythnet update for this feed was before the requested window"*, i.e., the returned update is provably the **first** update in `[minAllowedPublishTime, maxAllowedPublishTime]`.

The Stylus receiver instead does:

```rust
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
``` [2](#0-1) 

`prev_price_info.publish_time` is the **on-chain stored price's publish time** — whatever was last written by a prior `updatePriceFeeds` call — not the `prev_publish_time` field from the current Merkle-proven message. The `PriceFeedMessage` struct parsed at line 454 carries its own `prev_publish_time` field (consistent with the protocol definition used across all other chains), but the Stylus receiver never reads it for the uniqueness check. [3](#0-2) 

### Impact Explanation

Any transaction sender calling `parsePriceFeedUpdatesUnique` on the Stylus receiver can bypass the uniqueness guarantee:

1. Suppose the on-chain stored price has `publish_time = T0 < minAllowedPublishTime = T1`.
2. The Stylus check passes (`T0 < T1`, so `min_allowed_publish_time > prev_publish_time`).
3. The attacker submits update data containing a price update at `T1 + Δ` (not the first update in `[T1, T2]`) whose embedded `prevPublishTime` is `T1` (i.e., `prevPublishTime >= minAllowedPublishTime`).
4. The EVM check would reject this (`minAllowedPublishTime > prevPublishTime` fails). The Stylus check accepts it.
5. The caller receives a non-first price update in the range, defeating the uniqueness guarantee.

Protocols on Arbitrum Stylus that use `parsePriceFeedUpdatesUnique` for deterministic settlement (e.g., options expiry, TWAP anchoring, or any use-case requiring the canonical first price in a window) can be fed a later, attacker-selected price within the window. This enables price manipulation for any such integrator.

### Likelihood Explanation

The entry path is fully permissionless — any EOA or contract can call `parsePriceFeedUpdatesUnique` with crafted `updateData`. The condition for exploitation (on-chain stored price before the requested window) is the normal operating state for any fresh query. No privileged access, key compromise, or external oracle manipulation is required. Likelihood is high whenever a Stylus-deployed integrator relies on the uniqueness property.

### Recommendation

Replace the on-chain state lookup with the `prev_publish_time` field from the authenticated `PriceFeedMessage`:

```rust
if check_uniqueness {
    // Use the message-embedded prev_publish_time, not on-chain state
    if price_feed_message.prev_publish_time >= min_allowed_publish_time as i64 {
        return Err(PythReceiverError::PriceFeedNotFoundWithinRange);
    }
}
```

This mirrors the EVM implementation exactly and uses Wormhole-attested data rather than mutable on-chain state.

### Proof of Concept

**Setup:** Stylus receiver deployed; on-chain stored price for feed `F` has `publish_time = 999`.

**Attack:**
1. Pythnet emits two updates for feed `F` in slot range: update A at `publish_time=1000` (`prevPublishTime=999`) and update B at `publish_time=1005` (`prevPublishTime=1000`).
2. Attacker calls `parsePriceFeedUpdatesUnique(updateData_B, [F], 1000, 2000)` — submitting only update B.
3. **Stylus check:** `prev_publish_time = 999`, `min_allowed_publish_time = 1000`. Condition `1000 <= 999` is **false** → check passes. Update B (publish_time=1005) is returned.
4. **EVM check (correct):** `prevPublishTime = 1000`, `minAllowedPublishTime = 1000`. Condition `1000 > 1000` is **false** → update B is **rejected** because it is not the first update in the window.

The Stylus receiver returns update B (price at T=1005) as if it were the unique first update in `[1000, 2000]`, while the correct answer is update A (price at T=1000). The attacker chose which price the protocol receives. [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L238-258)
```text
        // Check if the priceId was requested and not already filled
        if (k < context.priceIds.length && context.priceFeeds[k].id == 0) {
            uint publishTime = uint(priceInfo.publishTime);
            if (
                publishTime >= context.minAllowedPublishTime &&
                publishTime <= context.maxAllowedPublishTime &&
                (!context.checkUniqueness ||
                    context.minAllowedPublishTime > prevPublishTime)
            ) {
                context.priceFeeds[k].id = priceId;
                context.priceFeeds[k].price.price = priceInfo.price;
                context.priceFeeds[k].price.conf = priceInfo.conf;
                context.priceFeeds[k].price.expo = priceInfo.expo;
                context.priceFeeds[k].price.publishTime = publishTime;
                context.priceFeeds[k].emaPrice.price = priceInfo.emaPrice;
                context.priceFeeds[k].emaPrice.conf = priceInfo.emaConf;
                context.priceFeeds[k].emaPrice.expo = priceInfo.expo;
                context.priceFeeds[k].emaPrice.publishTime = publishTime;
                context.slots[k] = merkleData.slot;
            }
        }
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L454-481)
```rust
                    let msg = from_slice::<byteorder::BE, Message>(&message_vec)
                        .map_err(|_| PythReceiverError::InvalidAccumulatorMessage)?;

                    match msg {
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
```
