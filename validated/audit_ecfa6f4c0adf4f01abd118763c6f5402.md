### Title
Provider Can Cherry-Pick Price Updates via Non-Unique Parse in Echo Callback - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.executeCallback` uses `parsePriceFeedUpdates` instead of `parsePriceFeedUpdatesUnique` when fetching the price for a request. Because Pyth publishes multiple updates per second, a registered provider can select among several valid, Wormhole-signed updates that all share the same `publishTime`, delivering a cherry-picked price to the consumer's callback rather than the canonical first price at that timestamp.

### Finding Description
In `executeCallback`, the contract resolves the price for a pending request by calling:

```solidity
// TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)   // min == max == req.publishTime
);
``` [1](#0-0) 

`parsePriceFeedUpdates(updateData, priceIds, T, T)` accepts **any** Pyth-signed update whose `publishTime == T`. Pyth Network is a high-frequency oracle that routinely publishes multiple attestations per second; all of them carry the same second-level `publishTime` but may carry different prices (e.g., successive ticks within the same second). The provider, who supplies the `updateData` argument, can freely choose which of those attestations to submit.

`parsePriceFeedUpdatesUnique` enforces the additional constraint `prevPublishTime < minPublishTime`, guaranteeing the returned update is the **first** one published at or after the requested timestamp, eliminating the provider's ability to select among contemporaneous updates.

The unresolved TODO on line 143 explicitly acknowledges this gap. [2](#0-1) 

The price ID validation only compares the first 8 bytes of each price ID (stored as `bytes8 priceIdPrefixes`), which means the provider also has a secondary degree of freedom: any price ID sharing the same 8-byte prefix passes the check. [3](#0-2) [4](#0-3) 

### Impact Explanation
A registered provider can deliver a price to the consumer's `_echoCallback` that is technically Wormhole-verified but is not the canonical first price at the requested timestamp. In DeFi applications that use Echo to trigger liquidations, settle options, or update collateral ratios, this allows the provider to select the price tick most favorable to their own positions, constituting oracle price manipulation. The consumer has no way to detect or reject this because the delivered `PriceFeed` is cryptographically valid.

### Likelihood Explanation
Any address can call `registerProvider` with zero fees and become a valid provider. [5](#0-4) 

Pyth publishes price updates at sub-second frequency; multiple updates per second with the same `publishTime` (unix seconds) are the norm, not the exception. The provider controls the `updateData` argument to `executeCallback` with no restriction beyond the 8-byte prefix check, making this trivially exploitable on any active Echo deployment.

### Recommendation
Replace `parsePriceFeedUpdates` with `parsePriceFeedUpdatesUnique` in `executeCallback`:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdatesUnique{
    value: pythFee
}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
```

Additionally, store and compare the full 32-byte price IDs rather than only the first 8 bytes, to eliminate the secondary prefix-collision degree of freedom.

### Proof of Concept
1. Alice deploys an Echo consumer and calls `requestPriceUpdatesWithCallback` for BTC/USD at `publishTime = T`, paying the required fee. The request is stored with `priceIdPrefixes[0] = first8Bytes(BTC_USD_ID)`.
2. At time `T`, Pythnet publishes two BTC/USD attestations both with `publishTime = T`: one at price $100,000 and one at $99,000.
3. Bob is a registered provider. He holds a short BTC position. He calls `executeCallback` supplying the `updateData` for the $99,000 attestation.
4. `parsePriceFeedUpdates` accepts it (valid Wormhole VAA, `publishTime == T`). Alice's callback receives `price = $99,000`.
5. Alice's contract (e.g., a lending protocol) uses this price to under-value BTC collateral and triggers an incorrect liquidation.
6. Bob profits from the liquidation while having paid only the standard provider fee. [6](#0-5)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L27-29)
```text
        // Store only first 8 bytes of each price ID to save gas
        bytes8[] priceIdPrefixes;
    }
```
