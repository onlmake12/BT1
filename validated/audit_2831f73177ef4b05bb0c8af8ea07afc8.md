### Title
Incomplete Price ID Verification Allows Fulfillment with Wrong Price Feeds — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.requestPriceUpdatesWithCallback` accepts full 32-byte price IDs from users but stores only the first 8 bytes (prefix) of each. `executeCallback` then verifies only those 8-byte prefixes. The remaining 24 bytes are silently discarded and never checked. A provider (or any caller after the exclusivity window) can fulfill a request with a different Pyth price feed that shares the same 8-byte prefix, causing the user's callback to receive price data for the wrong asset.

### Finding Description
In `requestPriceUpdatesWithCallback`, the full 32-byte `priceIds` are accepted as input but only 8-byte prefixes are stored:

```solidity
req.priceIdPrefixes = new bytes8[](priceIds.length);
for (uint8 i = 0; i < priceIds.length; i++) {
    bytes32 priceId = priceIds[i];
    bytes8 prefix;
    assembly { prefix := priceId }
    req.priceIdPrefixes[i] = prefix;
}
``` [1](#0-0) 

In `executeCallback`, only these 8-byte prefixes are verified against the caller-supplied `priceIds`:

```solidity
for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
    bytes32 priceId = priceIds[i];
    bytes8 prefix;
    assembly { prefix := priceId }
    if (prefix != req.priceIdPrefixes[i]) {
        revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
    }
}
``` [2](#0-1) 

The verified `priceIds` are then passed directly to `pyth.parsePriceFeedUpdates` and forwarded to the user's callback: [3](#0-2) [4](#0-3) 

The 24 bytes beyond the prefix are never stored and never checked. This is the direct analog to the original report: the function accepts full price IDs as inputs, validates only a partial representation, and silently ignores the rest — meaning the full identity of the requested feed is never enforced at fulfillment time.

### Impact Explanation
A user requests price updates for a specific asset (e.g., BTC/USD, price ID `0xE62DF6C8B4A85FE1A67DB44DC12DE5DB330F7AC66B72DC658AEF38D9E2F9D4C`). The provider (or any caller after the exclusivity period) can supply a different Pyth price ID whose first 8 bytes match. The `parsePriceFeedUpdates` call succeeds with the substitute ID, and the user's `_echoCallback` receives price data for the wrong asset. Any on-chain logic in the consumer contract (e.g., liquidation thresholds, collateral valuation) that relies on the callback data will operate on incorrect prices.

### Likelihood Explanation
The Pyth network publishes hundreds of price feeds. With only 8 bytes (64 bits) of prefix enforced, the probability of a natural collision among existing feeds is non-trivial. More critically, after the configurable `exclusivityPeriodSeconds` window expires, **any** external caller can invoke `executeCallback` — not just the assigned provider. An attacker who identifies a prefix collision among live Pyth feeds can permissionlessly fulfill the request with the wrong feed. The exclusivity period is configurable and can be set to zero. [5](#0-4) 

### Recommendation
Store the full 32-byte price IDs in the request struct instead of 8-byte prefixes. Replace `bytes8[] priceIdPrefixes` with `bytes32[] priceIds` in the `Request` struct and compare the full IDs in `executeCallback`. This eliminates the partial-verification gap entirely.

### Proof of Concept
1. User calls `requestPriceUpdatesWithCallback` with price ID `0xE62DF6C8...` (BTC/USD). Contract stores prefix `0xE62DF6C8B4A85FE1`.
2. Attacker identifies another Pyth price ID `0xE62DF6C8B4A85FE1_<different_24_bytes>` (a different asset).
3. After the exclusivity period, attacker calls `executeCallback` with the substitute price ID and valid update data for that feed.
4. The 8-byte prefix check passes (`0xE62DF6C8B4A85FE1 == 0xE62DF6C8B4A85FE1`).
5. `pyth.parsePriceFeedUpdates` returns price data for the substitute asset.
6. The user's `_echoCallback` receives and acts on wrong price data.

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L113-121)
```text
        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L128-141)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L144-153)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-179)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
```
