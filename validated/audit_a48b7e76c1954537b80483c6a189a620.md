### Title
`parseUpdateFromPayload` Incorrectly Encodes Zero-Valued Prices as `ApplicableButMissing` in Packed `triStateMap` Bitfield — (`File: lazer/contracts/evm/src/PythLazerLib.sol`)

---

### Summary

`PythLazerLib.parseUpdateFromPayload` uses `value != 0` to decide whether to call `_setPresent` or `_setApplicableButMissing` when encoding property state into the packed `triStateMap` bitfield. This is the wrong condition: a legitimately reported price of `0` (e.g., a crashed asset) is silently encoded as `ApplicableButMissing` instead of `Present`, causing `hasPrice()` to return `false` and `getPrice()` to revert for valid zero-price Lazer updates.

---

### Finding Description

`PythLazerStructs.Feed.triStateMap` is a `uint256` that packs 2 bits per property at bit positions `[2*p, 2*p+1]`, encoding one of three states: `NotApplicable` (0), `ApplicableButMissing` (1), or `Present` (2). [1](#0-0) 

The setter and getter helpers (`_setTriState`, `_hasValue`, `_isRequested`) implement the bitfield arithmetic correctly: [2](#0-1) [3](#0-2) 

However, `parseUpdateFromPayload` determines which state to write by checking whether the parsed numeric value is non-zero:

```solidity
(feed._price, pos) = parseFeedValueInt64(payload, pos);
if (feed._price != 0) {
    _setPresent(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
} else {
    _setApplicableButMissing(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
}
``` [4](#0-3) 

The same pattern is applied to every numeric property: `BestBidPrice`, `BestAskPrice`, `Confidence`, `EmaPrice`, `EmaConfidence`, and `PublisherCount`. [5](#0-4) [6](#0-5) 

The correct condition for `Present` is: **the property ID was present in the wire payload** (i.e., the parser reached this branch). The value `0` is a valid `int64`/`uint64` reading. Using `!= 0` as a proxy for "present" conflates the encoding of the state with the value itself — exactly the same class of error as the TypedMemView `sameType()` bug, where the wrong arithmetic operand caused a packed-type comparison to produce the wrong boolean.

Note that `Exponent` and `MarketSession` are handled correctly — they always call `_setPresent` regardless of value: [7](#0-6) 

---

### Impact Explanation

When a Lazer-signed update legitimately contains `price = 0` (e.g., a stablecoin depeg to zero, a token crash), the `triStateMap` is written with `ApplicableButMissing` for the `Price` slot instead of `Present`. Downstream:

- `hasPrice(feed)` returns `false` — consumers that gate on this will silently skip the update.
- `getPrice(feed)` reverts with `"Price is not present for the timestamp"`.

A DeFi protocol relying on `getPrice()` to trigger liquidations or collateral checks will fail to act on the most critical market event (price → 0), potentially leaving bad debt unresolved or allowing insolvent positions to persist. [8](#0-7) 

---

### Likelihood Explanation

The trigger condition — a Lazer-signed update with `price == 0` — is a legitimate market event (asset crashes to zero, stablecoin fully depegs). It does not require any privileged action beyond the Lazer data provider faithfully reporting the market price. The Lazer updater is listed as a valid entry point in scope. The bug is in the on-chain parsing logic, not in the signer's behavior.

---

### Recommendation

Replace the `!= 0` value-check with unconditional `_setPresent` calls for all properties that are parsed from the wire payload. The presence of a property in the update is determined by the parser reaching that branch, not by the value being non-zero:

```solidity
// Before (incorrect):
if (feed._price != 0) {
    _setPresent(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
} else {
    _setApplicableButMissing(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
}

// After (correct):
_setPresent(feed, uint8(PythLazerStructs.PriceFeedProperty.Price));
```

Apply the same fix to `BestBidPrice`, `BestAskPrice`, `Confidence`, `EmaPrice`, `EmaConfidence`, and `PublisherCount`.

---

### Proof of Concept

1. Lazer signer produces a valid signed update for a feed with `price = 0` (e.g., asset crashed).
2. Consumer calls `PythLazer.verifyUpdate(update)` → succeeds, returns `payload`.
3. Consumer calls `PythLazerLib.parseUpdateFromPayload(payload)` → `feed._price = 0`, so `_setApplicableButMissing` is called, writing `01` into bits `[0,1]` of `triStateMap`.
4. Consumer calls `PythLazerLib.hasPrice(feed)`:
   - `_hasValue(feed, 0)` → `(triStateMap >> 0) & 3 == 1` (ApplicableButMissing), not `2` (Present) → returns `false`.
5. Consumer calls `PythLazerLib.getPrice(feed)` → reverts: `"Price is not present for the timestamp"`.
6. The DeFi protocol fails to process the zero-price update; stale price remains in use. [9](#0-8) [4](#0-3)

### Citations

**File:** lazer/contracts/evm/src/PythLazerStructs.sol (L50-54)
```text
        // Slot 1: tri-state map (2 bits per property; encoded in this uint256)
        // Encoding per property p (0..N):
        //   bits [2*p, 2*p+1]: 00 NotApplicable, 01 ApplicableButMissing, 10 Present, 11 Reserved
        // Capacity with uint256: 256 / 2 = 128 properties supported
        uint256 triStateMap;
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L9-22)
```text
    function _setTriState(
        PythLazerStructs.Feed memory feed,
        uint8 propId,
        PythLazerStructs.PropertyState state
    ) private pure {
        // Build a mask with zeros at the target 2-bit window and ones elsewhere
        //   uint256(3) is binary 11; shift it left into the window for this property
        //   ~ inverts the bits to create a clearing mask for just that window
        uint256 mask = ~(uint256(3) << (2 * propId));
        // Clear the window, then OR-in the desired state shifted into position
        feed.triStateMap =
            (feed.triStateMap & mask) |
            (uint256(uint8(state)) << (2 * propId));
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L42-58)
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

    function _isRequested(
        PythLazerStructs.Feed memory feed,
        uint8 propId
    ) private pure returns (bool) {
        // Requested if state != NotApplicable (i.e., any non-zero)
        return ((feed.triStateMap >> (2 * propId)) & 3) != 0;
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L252-264)
```text
                if (property == PythLazerStructs.PriceFeedProperty.Price) {
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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L267-312)
```text
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.BestBidPrice
                ) {
                    (feed._bestBidPrice, pos) = parseFeedValueInt64(
                        payload,
                        pos
                    );
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

                    // Best Ask Price Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.BestAskPrice
                ) {
                    (feed._bestAskPrice, pos) = parseFeedValueInt64(
                        payload,
                        pos
                    );
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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L344-351)
```text
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.Exponent
                ) {
                    (feed._exponent, pos) = parseFeedValueInt16(payload, pos);
                    _setPresent(
                        feed,
                        uint8(PythLazerStructs.PriceFeedProperty.Exponent)
                    );
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L354-371)
```text
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.Confidence
                ) {
                    (feed._confidence, pos) = parseFeedValueUint64(
                        payload,
                        pos
                    );
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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L829-839)
```text
    /// @notice Get price (reverts if not exists)
    function getPrice(
        PythLazerStructs.Feed memory feed
    ) public pure returns (int64) {
        require(
            isPriceRequested(feed),
            "Price is not requested for the timestamp"
        );
        require(hasPrice(feed), "Price is not present for the timestamp");
        return feed._price;
    }
```
