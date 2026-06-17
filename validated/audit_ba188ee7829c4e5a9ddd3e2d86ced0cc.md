### Title
Unbounded Quadratic Gas in `parseTwapPriceFeedUpdates` Nested Loop — (`target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`)

---

### Summary

`parseTwapPriceFeedUpdates` contains a nested loop whose total iterations equal `priceIds.length × startPriceIds.length`. The fee charged (`getTwapUpdateFee(updateData)`) is computed solely from the VAA blob and scales with `numUpdates` (max 255), **not** with the caller-supplied `priceIds.length`. An unprivileged caller can pass an arbitrarily large `priceIds[]` array while paying a fixed fee, causing gas consumption that grows without bound relative to the fee paid.

---

### Finding Description

The outer loop iterates over every caller-supplied `priceIds[i]`: [1](#0-0) 

For each outer iteration, the inner loop scans `startPriceIds[]` linearly until a match is found (or exhausted): [2](#0-1) 

`startPriceIds` is populated from the VAA's `numUpdates` field, which is a `uint8` (max 255): [3](#0-2) 

The fee check uses only `updateData`, not `priceIds`: [4](#0-3) 

The function signature confirms `priceIds` is entirely caller-controlled with no length cap: [5](#0-4) 

---

### Impact Explanation

With `numUpdates = 255` and `priceIds.length = 1000`, the worst-case inner loop count is ~255,000 `bytes32` comparisons. At ~50–100 gas each, this yields ~12–25 M gas — approaching Ethereum's ~30 M block gas limit in a single call. The attacker pays a fixed fee (255 × `singleUpdateFee`) regardless of `priceIds.length`, creating an amplification: the fee is constant while gas scales linearly with `priceIds.length`. A single such transaction can consume most of a block, delaying or blocking concurrent legitimate TWAP queries.

---

### Likelihood Explanation

- Valid TWAP VAAs with many entries are routinely available from Hermes (Pyth publishes hundreds of feeds per slot).
- No privileged role is required; the function is `external payable`.
- The attacker can repeat valid price IDs (e.g., 1000 copies of the same ID that appears last in the VAA) to maximize inner-loop iterations while ensuring the final revert-check at lines 579–583 passes. [6](#0-5) 

---

### Recommendation

1. **Cap `priceIds.length`** to a protocol-defined maximum (e.g., `numUpdates` from the VAA, since requesting more IDs than the VAA contains is nonsensical).
2. **Include `priceIds.length` in the fee calculation** so the fee scales with the actual work performed.
3. **Replace the linear scan** with a mapping built once from `startPriceIds` (O(M) setup, O(1) lookup), eliminating the quadratic term entirely.

---

### Proof of Concept

```solidity
// Attacker obtains a valid TWAP VAA pair from Hermes with numUpdates=255.
// Suppose priceId X appears at index 254 (last) in the VAA.
bytes32[] memory ids = new bytes32[](1000);
for (uint i = 0; i < 1000; i++) ids[i] = X; // all valid, all worst-case position

pyth.parseTwapPriceFeedUpdates{value: getTwapUpdateFee(updateData)}(
    updateData, // 2-element array, each blob has numUpdates=255
    ids         // 1000 entries → 1000 × 255 = 255,000 inner iterations
);
// Gas consumed ≈ 12–25 M; fee paid = 255 × singleUpdateFee (fixed).
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L440-470)
```text
        uint8 numUpdates;
        bytes calldata encoded;
        // Extract and validate the header for start data
        (offset, updateType) = extractUpdateTypeFromAccumulatorHeader(
            updateData
        );

        if (updateType != UpdateType.WormholeMerkle) {
            revert PythErrors.InvalidUpdateData();
        }

        (
            offset,
            digest,
            numUpdates,
            encoded,
            // slot ignored

        ) = extractWormholeMerkleHeaderDigestAndNumUpdatesAndEncodedAndSlotFromAccumulatorUpdate(
            updateData,
            offset
        );

        // Add additional validation before extracting TWAP price info
        if (offset >= updateData.length) {
            revert PythErrors.InvalidUpdateData();
        }

        // Initialize arrays to store all price infos and ids from this update
        twapPriceInfos = new PythStructs.TwapPriceInfo[](numUpdates);
        priceIds = new bytes32[](numUpdates);
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L491-499)
```text
    function parseTwapPriceFeedUpdates(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    )
        external
        payable
        override
        returns (PythStructs.TwapPriceFeed[] memory twapPriceFeeds)
    {
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L505-506)
```text
        uint requiredFee = getTwapUpdateFee(updateData);
        if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L547-558)
```text
        for (uint i = 0; i < priceIds.length; i++) {
            bytes32 requestedPriceId = priceIds[i];
            int startIdx = -1;

            // Find the index of this price ID in the startPriceIds array
            // (which is the same as the endPriceIds array based on our validation above)
            for (uint j = 0; j < startPriceIds.length; j++) {
                if (startPriceIds[j] == requestedPriceId) {
                    startIdx = int(j);
                    break;
                }
            }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L579-583)
```text
        for (uint k = 0; k < priceIds.length; k++) {
            if (twapPriceFeeds[k].id == 0) {
                revert PythErrors.PriceFeedNotFoundWithinRange();
            }
        }
```
