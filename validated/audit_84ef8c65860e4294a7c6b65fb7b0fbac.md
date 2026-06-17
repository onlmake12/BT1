### Title
Zero-Value Price Classified as Missing Instead of Present — (`File: lazer/contracts/evm/src/PythLazerLib.sol`)

### Summary
`PythLazerLib.parseUpdateFromPayload` determines whether a numeric feed property (price, confidence, EMA price, etc.) is "Present" by checking `value != 0`. Because zero is a valid numeric value, any legitimately-published zero-valued property is silently downgraded to `ApplicableButMissing`, causing all downstream `getPrice()` / `getConfidence()` / `getEmaPrice()` calls to revert even though the data was correctly included in the signed payload.

### Finding Description

In `parseUpdateFromPayload`, every numeric property except `Exponent` uses the pattern:

```solidity
(feed._price, pos) = parseFeedValueInt64(payload, pos);
if (feed._price != 0) {
    _setPresent(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
} else {
    _setApplicableButMissing(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
}
``` [1](#0-0) 

The same pattern is repeated for `BestBidPrice`, `BestAskPrice`, `PublisherCount`, `Confidence`, `EmaPrice`, and `EmaConfidence`: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) 

The tri-state model has three states: `NotApplicable (0)`, `ApplicableButMissing (1)`, `Present (2)`. The correct way to distinguish "property was included in the payload but its value happens to be zero" from "property was not included" is to use the protocol's own encoding (e.g., the explicit `exists` byte already used for `FundingRate`, `FundingTimestamp`, `FundingRateInterval`, and `FeedUpdateTimestamp`): [8](#0-7) 

Instead, the affected properties conflate "value is zero" with "value is absent."

The safe getter `getPrice` enforces both `isPriceRequested` and `hasPrice`:

```solidity
require(isPriceRequested(feed), "Price is not requested for the timestamp");
require(hasPrice(feed), "Price is not present for the timestamp");
return feed._price;
``` [9](#0-8) 

When `feed._price == 0`, `isPriceRequested` returns `true` (state is `ApplicableButMissing ≠ 0`) but `hasPrice` returns `false` (state is not `Present`), so the call reverts with "Price is not present for the timestamp" — even though the price was correctly signed and transmitted.

### Impact Explanation

Any on-chain consumer that calls `getPrice()`, `getConfidence()`, `getEmaPrice()`, `getEmaConfidence()`, `getBestBidPrice()`, `getBestAskPrice()`, or `getPublisherCount()` on a feed whose value is exactly zero will receive a revert. This is a liveness failure: critical protocol operations (liquidations, settlement, collateral checks) that depend on reading a zero-valued price will be permanently blocked for that update cycle, even though the Lazer network correctly signed and delivered the data.

**Impact: High** — liveness failure for consumers during zero-price events (e.g., a de-pegged stablecoin, a token crash, or a zero confidence interval).

### Likelihood Explanation

**Likelihood: Low** — prices of exactly zero are rare in normal market conditions, but are realistic during extreme events (token crashes, de-pegs). `PublisherCount == 0` and `Confidence == 0` are more plausible edge cases. The bug is deterministic: every occurrence of a zero value in a signed payload triggers the failure.

### Recommendation

Replace the value-based presence check with an explicit encoding approach (matching the pattern already used for `FundingRate` etc.), or unconditionally call `_setPresent` when the property tag is present in the payload, since the property's inclusion in the payload — not its value — determines presence:

```diff
- (feed._price, pos) = parseFeedValueInt64(payload, pos);
- if (feed._price != 0) {
-     _setPresent(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
- } else {
-     _setApplicableButMissing(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
- }
+ (feed._price, pos) = parseFeedValueInt64(payload, pos);
+ _setPresent(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
```

Apply the same fix to `BestBidPrice`, `BestAskPrice`, `PublisherCount`, `Confidence`, `EmaPrice`, and `EmaConfidence`.

### Proof of Concept

1. Lazer network signs a payload for feed `X` with `price = 0` (e.g., a crashed token).
2. A relayer submits the signed payload on-chain.
3. A consumer contract calls `PythLazerLib.parseUpdateFromPayload(payload)`.
4. Inside `parseUpdateFromPayload`, the `Price` property tag is read from the payload, `parseFeedValueInt64` returns `0`, and the branch `feed._price != 0` is `false` → `_setApplicableButMissing` is called → `triStateMap` encodes state `1` for the Price property.
5. The consumer calls `getPrice(feed)`:
   - `isPriceRequested(feed)` → `(triStateMap >> 0) & 3 == 1 != 0` → `true`
   - `hasPrice(feed)` → `(triStateMap >> 0) & 3 == 1 != 2` → `false`
   - Reverts: `"Price is not present for the timestamp"`
6. The consumer's liquidation / settlement logic is blocked despite the Lazer network having correctly published the zero price. [10](#0-9) [11](#0-10)

### Citations

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L42-50)
```text
    function _hasValue(
        PythLazerStructs.Feed memory feed,
        uint8 propId
    ) private pure returns (bool) {
        // Shift the property window down to bits [0,1], mask with 0b11 (3), compare to Present (2)
        return
            ((feed.triStateMap >> (2 * propId)) & 3) ==
            uint256(uint8(PythLazerStructs.PropertyState.Present));
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L253-264)
```text
                    (feed._price, pos) = parseFeedValueInt64(payload, pos);
                    if (feed._price != 0) {
                        _setPresent(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Price)
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Price)
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L274-288)
```text
                    if (feed._bestBidPrice != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestBidPrice
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestBidPrice
                            )
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L298-312)
```text
                    if (feed._bestAskPrice != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestAskPrice
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestAskPrice
                            )
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L323-341)
```text
                    if (feed._publisherCount != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .PublisherCount
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .PublisherCount
                            )
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L361-371)
```text
                    if (feed._confidence != 0) {
                        _setPresent(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Confidence)
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Confidence)
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L377-397)
```text
                    uint8 exists;
                    (exists, pos) = parseFeedValueUint8(payload, pos);
                    if (exists != 0) {
                        (feed._fundingRate, pos) = parseFeedValueInt64(
                            payload,
                            pos
                        );
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.FundingRate
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.FundingRate
                            )
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L486-496)
```text
                    if (feed._emaPrice != 0) {
                        _setPresent(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.EmaPrice)
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.EmaPrice)
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L505-519)
```text
                    if (feed._emaConfidence != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.EmaConfidence
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.EmaConfidence
                            )
                        );
                    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L566-571)
```text
    /// @notice Check if price exists
    function hasPrice(
        PythLazerStructs.Feed memory feed
    ) public pure returns (bool) {
        return _hasValue(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L833-839)
```text
        require(
            isPriceRequested(feed),
            "Price is not requested for the timestamp"
        );
        require(hasPrice(feed), "Price is not present for the timestamp");
        return feed._price;
    }
```
