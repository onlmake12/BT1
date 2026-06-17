### Title
Missing Empty `priceIds` Array Validation in `Echo.requestPriceUpdatesWithCallback` Allows Zero-Feed Callback Execution — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol`'s `requestPriceUpdatesWithCallback` accepts a user-supplied `priceIds` array without checking that it is non-empty. An unprivileged caller can submit a request with `priceIds.length == 0`, which stores a request with an empty prefix array, bypasses all per-feed fee accounting, and ultimately triggers the consumer's `_echoCallback` with an empty `PriceFeed[]` array — delivering no verified price data to the callback target.

---

### Finding Description

In `requestPriceUpdatesWithCallback`, the only array-length guard is an upper-bound check:

```solidity
if (priceIds.length > MAX_PRICE_IDS) {
    revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
}
``` [1](#0-0) 

There is no lower-bound check (`priceIds.length == 0`). The request is stored with an empty `priceIdPrefixes` array:

```solidity
req.priceIdPrefixes = new bytes8[](priceIds.length);
``` [2](#0-1) 

In `executeCallback`, the length-equality check passes trivially when both sides are zero:

```solidity
require(
    priceIds.length == req.priceIdPrefixes.length,
    "Price IDs length mismatch"
);
``` [3](#0-2) 

The inner prefix-comparison loop is skipped entirely, and `parsePriceFeedUpdates` is called with an empty `priceIds` slice, returning an empty `PriceFeed[]`. The consumer callback is then invoked with that empty array:

```solidity
IEchoConsumer(req.requester)._echoCallback{
    gas: req.callbackGasLimit
}(sequenceNumber, priceFeeds)
``` [4](#0-3) 

By contrast, the analogous `Scheduler._validateSubscriptionParams` explicitly rejects empty price-ID arrays:

```solidity
if (params.priceIds.length == 0) {
    revert SchedulerErrors.EmptyPriceIds();
}
``` [5](#0-4) 

Echo has no equivalent guard.

---

### Impact Explanation

1. **Callback with no price data**: Any consumer contract whose `_echoCallback` implementation does not defensively check `priceFeeds.length > 0` will silently execute with zero verified prices. A DeFi protocol that reads `priceFeeds[0]` inside the callback will revert or, worse, use a default/stale value.
2. **Fee undercharge**: The per-feed component of the fee (`priceIds.length * feePerFeedInWei`) is zero, so the requester pays less than the intended cost for a real price-update request.
3. **Provider fee accounting skew**: `req.fee` is set to `msg.value - pythFeeInWei`; with an empty request the provider's accrued balance is incremented for work that produced no price data.

---

### Likelihood Explanation

The entry point is fully permissionless — any EOA or contract can call `requestPriceUpdatesWithCallback`. No special role, key, or governance access is required. A malicious actor can deliberately pass `new bytes32[](0)` to grief consumer contracts or reduce fees; an integrator can do so accidentally. The exclusivity-period check does not protect against this because it only constrains `providerToCredit`, not `priceIds`.

---

### Recommendation

Add a lower-bound guard immediately after the upper-bound check:

```solidity
if (priceIds.length == 0) {
    revert EmptyPriceIds();
}
if (priceIds.length > MAX_PRICE_IDS) {
    revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
}
```

This mirrors the existing pattern in `Scheduler._validateSubscriptionParams`. [6](#0-5) 

---

### Proof of Concept

```solidity
// Attacker calls with empty priceIds
bytes32[] memory emptyIds = new bytes32[](0);
uint64 seq = echo.requestPriceUpdatesWithCallback{value: baseFee}(
    registeredProvider,
    block.timestamp,
    emptyIds,        // ← no revert, no guard
    callbackGasLimit
);

// Anyone (or the provider) calls executeCallback with empty priceIds
bytes[] memory updateData = ...; // any valid Pyth update blob
echo.executeCallback{value: pythFee}(
    registeredProvider,
    seq,
    updateData,
    emptyIds         // ← length matches stored 0-length prefix array
);
// Consumer._echoCallback is invoked with priceFeeds.length == 0
```

The consumer contract receives a callback carrying zero `PriceFeed` entries, with no on-chain revert to signal the anomaly.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L70-72)
```text
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L87-98)
```text
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L124-127)
```text
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-179)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L160-163)
```text
        // No zero‐feed subscriptions
        if (params.priceIds.length == 0) {
            revert SchedulerErrors.EmptyPriceIds();
        }
```
