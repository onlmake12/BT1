### Title
Truncated Price Feed ID Validation in `Echo.executeCallback` Allows Feed Substitution — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol` stores only the **first 8 bytes** of each requested price feed ID at request time and validates only those 8 bytes during `executeCallback`. An unprivileged caller (after the exclusivity period) can supply a different 32-byte price feed ID that shares the same 8-byte prefix as the originally requested feed, along with valid Pyth update data for that substitute feed. The consumer's `_echoCallback` then receives price data for the wrong asset.

---

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, the contract stores only a `bytes8` prefix of each price feed ID:

```solidity
// Echo.sol lines 91–97
bytes32 priceId = priceIds[i];
bytes8 prefix;
assembly {
    prefix := priceId
}
req.priceIdPrefixes[i] = prefix;
``` [1](#0-0) 

During `executeCallback`, the validation compares only these 8 bytes against the caller-supplied `priceIds`:

```solidity
// Echo.sol lines 128–141
for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
    bytes32 priceId = priceIds[i];
    bytes8 prefix;
    assembly { prefix := priceId }
    if (prefix != req.priceIdPrefixes[i]) {
        revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
    }
}
``` [2](#0-1) 

The remaining **24 bytes** of the 32-byte price feed ID are never checked. After the prefix check passes, the caller-supplied `priceIds` array is passed directly to `pyth.parsePriceFeedUpdates`:

```solidity
// Echo.sol lines 144–153
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(
    updateData,
    priceIds,                              // ← attacker-controlled full IDs
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
``` [3](#0-2) 

`parsePriceFeedUpdates` will successfully return price data for whatever 32-byte ID is in `priceIds`, as long as the `updateData` contains a valid Pyth-signed update for that feed. The consumer's `_echoCallback` is then invoked with price data for the substituted asset.

The exclusivity period check only restricts *which provider* can call `executeCallback` during the window — after it expires, any address may call it:

```solidity
// Echo.sol lines 113–121
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
``` [4](#0-3) 

---

### Impact Explanation

A consumer contract that uses Echo for price-triggered callbacks (e.g., collateral valuation, liquidation, settlement) will receive price data for the wrong asset. Depending on the consumer's logic, this can cause:

- Incorrect collateral valuations (under- or over-collateralized positions accepted)
- Liquidations triggered on healthy positions (or blocked on unhealthy ones)
- Settlement at a manipulated price

The impact is equivalent to the external report: price update data is not validated to match the asset the consuming contract actually requested.

---

### Likelihood Explanation

Pyth publishes 500+ price feeds. With 64-bit (8-byte) prefixes, the birthday-paradox probability of a collision among 500 feeds is non-trivial (~0.7%). An attacker can enumerate all published feed IDs from Hermes, find one sharing the same 8-byte prefix as a target feed, and fetch a valid signed update for it. After the exclusivity period (a configurable number of seconds), any address can call `executeCallback` — no privileged role is required.

---

### Recommendation

Store and validate the **full 32-byte price feed ID** rather than an 8-byte prefix. Replace `bytes8 priceIdPrefixes` in the request struct with `bytes32[] priceIds` (or a fixed-size array), and compare the full ID in `executeCallback`:

```solidity
// Store full ID at request time
req.priceIds[i] = priceIds[i];

// Validate full ID in executeCallback
if (priceIds[i] != req.priceIds[i]) {
    revert InvalidPriceIds(priceIds[i], req.priceIds[i]);
}
```

This mirrors the correct pattern used in `Scheduler.sol`, which passes `params.priceIds` (full 32-byte IDs stored at subscription creation) directly to `parsePriceFeedUpdatesWithConfig` without any truncation. [5](#0-4) 

---

### Proof of Concept

1. Alice calls `requestPriceUpdatesWithCallback` requesting feed `0xABCDEF1234567890<24 more bytes for ETH/USD>` at `publishTime = T`.
2. The contract stores `priceIdPrefixes[0] = 0xABCDEF1234567890`.
3. Attacker finds feed `0xABCDEF1234567890<different 24 bytes, e.g., a low-value token>` in Pyth's catalog.
4. Attacker fetches a valid Hermes-signed update for the substitute feed at timestamp `T`.
5. After the exclusivity period, attacker calls `executeCallback(providerToCredit, sequenceNumber, substituteUpdateData, [substituteFullId])`.
6. The 8-byte prefix check passes (`0xABCDEF1234567890 == 0xABCDEF1234567890`).
7. `parsePriceFeedUpdates` succeeds, returning the substitute asset's price.
8. Alice's consumer contract receives the wrong price in `_echoCallback` and acts on it.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L91-98)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L308-319)
```text
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );
```
