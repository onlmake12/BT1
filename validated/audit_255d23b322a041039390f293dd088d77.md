### Title
Excess `msg.value` Permanently Locked in Contract Across Multiple Payable Price-Feed Functions - (File: target_chains/ethereum/contracts/contracts/pyth/Pyth.sol)

### Summary
Three payable functions in `Pyth.sol` — `updatePriceFeeds()`, `parsePriceFeedUpdatesWithConfig()`, and `parseTwapPriceFeedUpdates()` — enforce a minimum fee via `msg.value < requiredFee` but silently absorb any excess ETH into the contract balance. The excess is irrecoverable by the caller and can only be extracted by Pyth governance via a `WithdrawFee` VAA. An analogous issue exists in `Entropy.sol`'s `requestHelper()`, where excess `msg.value` above the required fee is silently credited to `accruedPythFeesInWei` (Pyth's own fee pool) rather than refunded to the caller.

### Finding Description

**`Pyth.sol` — three affected entry points:**

`updatePriceFeeds()` computes `requiredFee` and reverts if `msg.value` is too low, but performs no refund:

```solidity
// Pyth.sol L77-78
uint requiredFee = getTotalFee(totalNumUpdates);
if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
// function ends — excess msg.value stays in contract
```

`parsePriceFeedUpdatesWithConfig()` has the identical pattern:

```solidity
// Pyth.sol L336-337
if (msg.value < getUpdateFee(updateData))
    revert PythErrors.InsufficientFee();
// no refund
```

`parseTwapPriceFeedUpdates()` likewise:

```solidity
// Pyth.sol L505-506
uint requiredFee = getTwapUpdateFee(updateData);
if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
// no refund
```

All excess ETH accumulates in the contract's raw balance. It is only recoverable via a signed governance VAA (`WithdrawFee` action), meaning the user has no self-service path to reclaim it.

**`Entropy.sol` — secondary affected entry point:**

`requestHelper()` (called by every `requestV2` / `requestWithCallback` variant) enforces the minimum fee but then credits the entire `msg.value - providerFee` to Pyth's own fee pool:

```solidity
// Entropy.sol L235, L238-239
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
```

Any overpayment silently inflates `accruedPythFeesInWei` rather than being returned to the caller.

By contrast, `PythLazer.sol`'s `verifyUpdate()` correctly refunds excess:

```solidity
// PythLazer.sol L74-77
require(msg.value >= verification_fee, "Insufficient fee provided");
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
```

This confirms the Pyth codebase is aware of the pattern and has applied it selectively, leaving the higher-traffic price-feed and entropy paths unprotected.

### Impact Explanation
Any caller of `updatePriceFeeds`, `parsePriceFeedUpdatesWithConfig`, `parseTwapPriceFeedUpdates`, or any `requestV2`/`requestWithCallback` variant who sends more ETH than the exact required fee permanently loses the surplus. For `Pyth.sol` functions the surplus is locked in the contract until a governance action is executed. For `Entropy.sol` the surplus is silently donated to Pyth's fee pool. In neither case is the caller refunded. This is a direct, quantifiable loss of user funds with no recovery path available to the user.

### Likelihood Explanation
The likelihood is medium. Callers are expected to query `getUpdateFee()` / `getFeeV2()` before submitting, but:
- Fee values can change between the query block and the execution block (governance can update `singleUpdateFeeInWei` or `pythFeeInWei`).
- Integrators commonly add a small buffer to `msg.value` to avoid reverts from fee increases, which is a standard defensive pattern that silently triggers this loss.
- Smart-contract wrappers (e.g., `PythAggregatorV3`) that forward `address(this).balance` as `msg.value` can send arbitrarily large surpluses.
- The Entropy `requestHelper` path is called by every randomness request, making it a high-frequency target.

### Recommendation
Apply the same refund pattern already used in `PythLazer.sol` to all affected functions:

```solidity
// After the fee check in updatePriceFeeds / parsePriceFeedUpdatesWithConfig / parseTwapPriceFeedUpdates:
if (msg.value > requiredFee) {
    (bool ok, ) = payable(msg.sender).call{value: msg.value - requiredFee}("");
    require(ok, "refund failed");
}
```

For `Entropy.sol`'s `requestHelper`, replace the current catch-all accumulation with an explicit split:

```solidity
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += _state.pythFeeInWei;
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool ok, ) = payable(msg.sender).call{value: excess}("");
    require(ok, "refund failed");
}
```

Alternatively, revert on any `msg.value` that exceeds the required fee.

### Proof of Concept

**Pyth.sol path:**

1. Governance sets `singleUpdateFeeInWei = 1 wei`. User queries `getUpdateFee(updateData)` → returns `N wei`.
2. User submits `updatePriceFeeds{value: N + 1 ether}(updateData)` (e.g., adding a buffer).
3. `requiredFee = N`. Check passes. Function returns. No refund issued.
4. `1 ether` is now locked in the `Pyth` proxy contract balance.
5. Only a governance `WithdrawFee` VAA signed by the Pyth governance emitter can extract it — the user has no recourse.

**Entropy.sol path:**

1. User calls `getFeeV2(provider, gasLimit)` → returns `R wei` (`providerFee + pythFee`).
2. User submits `requestV2{value: R + 0.5 ether}(provider, gasLimit)`.
3. Inside `requestHelper`: `providerInfo.accruedFeesInWei += providerFee`; `_state.accruedPythFeesInWei += (R + 0.5 ether - providerFee)` = `pythFee + 0.5 ether`.
4. The `0.5 ether` surplus is silently credited to Pyth's fee pool. The user cannot recover it. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L77-79)
```text
        uint requiredFee = getTotalFee(totalNumUpdates);
        if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L336-338)
```text
        if (msg.value < getUpdateFee(updateData))
            revert PythErrors.InsufficientFee();

```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L505-507)
```text
        uint requiredFee = getTwapUpdateFee(updateData);
        if (msg.value < requiredFee) revert PythErrors.InsufficientFee();

```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-239)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L74-77)
```text
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
