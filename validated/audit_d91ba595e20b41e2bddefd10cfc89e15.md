### Title
Partial Price ID Validation in `executeCallback` Allows Provider to Substitute a Different Price Feed — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.sol`'s `executeCallback` validates only the first 8 bytes (`bytes8` prefix) of each caller-supplied `priceIds` entry against the stored request, rather than the full 32-byte price ID. A provider can supply a different price ID that shares the same 8-byte prefix as the originally requested one, causing the consumer callback to receive price data for a different asset than what was requested.

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, only the first 8 bytes of each price ID are stored: [1](#0-0) 

```solidity
req.priceIdPrefixes = new bytes8[](priceIds.length);
for (uint8 i = 0; i < priceIds.length; i++) {
    bytes32 priceId = priceIds[i];
    bytes8 prefix;
    assembly { prefix := priceId }
    req.priceIdPrefixes[i] = prefix;
}
```

When `executeCallback` is later called, the validation checks only those same 8 bytes: [2](#0-1) 

```solidity
for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
    bytes32 priceId = priceIds[i];
    bytes8 prefix;
    assembly { prefix := priceId }
    if (prefix != req.priceIdPrefixes[i]) {
        revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
    }
}
```

The caller-supplied `priceIds` (not the originally requested ones) are then forwarded directly to Pyth: [3](#0-2) 

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{value: pythFee}(
    updateData,
    priceIds,          // ← provider-supplied, only 8/32 bytes validated
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
```

The consumer callback then receives `priceFeeds` for the provider-supplied IDs, not the originally requested ones.

This is structurally identical to the SP1Blobstream bug: a value (`priceIds`) is passed as a separate input to the fulfillment function but is not fully validated against the data structure it is supposed to represent (the original 32-byte price IDs stored at request time). Only 8 of 32 bytes are checked, leaving 24 bytes unvalidated.

### Impact Explanation

A provider (or any caller after the exclusivity period) who can find two Pyth price IDs sharing the same 8-byte prefix can:

1. Fulfill a request for price feed A by supplying valid Pyth update data for price feed B (same prefix, different suffix).
2. The consumer's `_echoCallback` receives `PriceFeed[]` for feed B instead of feed A.
3. Any on-chain logic in the consumer that acts on the returned price (e.g., liquidation thresholds, collateral ratios) operates on the wrong asset's price.

### Likelihood Explanation

**Low.** Pyth price IDs are 32-byte values assigned by Pyth Network. With ~2,000–5,000 active feeds, the birthday-collision probability for any two feeds sharing the same 8-byte prefix is approximately `n²/(2·2^64)` ≈ negligible today. However:

- The stored prefix is only 8 bytes (64 bits), not the full 32 bytes.
- As Pyth expands to tens of thousands of feeds, collision probability grows.
- The design flaw is structural: the contract was designed to save gas by storing only a prefix, but this creates an incomplete binding between the request and its fulfillment.
- Any attacker who controls a provider registration can monitor for prefix collisions across all registered Pyth feeds.

### Recommendation

Store and validate the full 32-byte price IDs at request time instead of only the 8-byte prefix. The gas savings from truncation do not justify the incomplete binding:

```solidity
// Store full price IDs
req.priceIds = priceIds; // bytes32[]

// Validate full price IDs in executeCallback
for (uint8 i = 0; i < req.priceIds.length; i++) {
    if (priceIds[i] != req.priceIds[i]) {
        revert InvalidPriceIds(priceIds[i], req.priceIds[i]);
    }
}
```

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback(provider, T, [ID_A], gasLimit)` where `ID_A = 0xAABBCCDDEEFF00001111...`.
2. Contract stores `priceIdPrefixes[0] = 0xAABBCCDDEEFF0000` (first 8 bytes).
3. Attacker finds or constructs `ID_B = 0xAABBCCDDEEFF00002222...` (same prefix, different suffix) — a different Pyth price feed.
4. Attacker calls `executeCallback(provider, seqNum, updateDataForID_B, [ID_B])`.
5. Prefix check passes (`0xAABBCCDDEEFF0000 == 0xAABBCCDDEEFF0000`).
6. `parsePriceFeedUpdates` returns price data for `ID_B`.
7. Consumer's `_echoCallback` receives price data for `ID_B` instead of `ID_A`. [4](#0-3)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-153)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );
```
