### Title
No Penalty Mechanism for Unfulfilled Echo Callbacks Leaves Requests Permanently Stuck with Locked Funds - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo` contract collects fees upfront from users requesting price updates with callbacks. When gas prices spike after a request is made, the pre-paid fee may become insufficient to cover the actual fulfillment cost. Because there is no penalty for the assigned provider skipping the callback and no refund path for users, requests can be permanently stuck with user funds locked in the contract. The contract's own code acknowledges this gap with an explicit TODO comment.

---

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback()` collects a fee from the user at request time, calculated as:

```
fee = pythFeeInWei + baseFeeInWei + (feePerFeedInWei × numFeeds) + (feePerGasInWei × callbackGasLimit)
```

The `feePerGasInWei` is a static rate set by the provider at registration time. The fee is locked in the contract immediately:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
_state.accruedFeesInWei += _state.pythFeeInWei;
```

The `executeCallback()` function enforces an exclusivity window during which only the assigned provider may fulfill:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(providerToCredit == req.provider, "Only assigned provider during exclusivity period");
}
```

After the exclusivity period, any registered provider may fulfill. However, there is **no penalty** for the assigned provider failing to fulfill, and **no refund** for the user if no provider ever calls `executeCallback()`. The contract itself acknowledges this with a TODO at lines 157–159:

```solidity
// TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
// This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
// with time in order to ensure that the callback eventually gets executed.
```

There is also no cancellation or refund function anywhere in the contract.

---

### Impact Explanation

When gas prices spike significantly after a request is made:

1. The assigned provider's actual fulfillment cost exceeds `req.fee`, making fulfillment unprofitable.
2. The provider skips the callback with no on-chain consequence.
3. After the exclusivity period, other providers face the same unprofitable economics and also skip.
4. The request is permanently stuck: the user's fee is locked in the contract forever, and the consumer contract never receives its price update.
5. Consumer contracts that depend on the price update (e.g., for settlement, liquidation, or game resolution) are left in a broken state.

Additionally, because `NUM_REQUESTS = 32` slots are used with a ring-buffer-style allocation, a flood of stuck requests can force active requests into the `requestsOverflow` mapping, increasing gas costs for all subsequent operations.

---

### Likelihood Explanation

Gas price spikes are a well-documented, recurring phenomenon on Ethereum and EVM-compatible chains — they occur during periods of high network activity (NFT mints, token launches, market volatility). The `feePerGasInWei` is a static provider-configured value that cannot automatically track real-time gas prices. Any request submitted during a low-gas period that remains unfulfilled when gas prices spike becomes permanently unprofitable to fulfill. This is not a theoretical edge case; it is a predictable consequence of the static fee model combined with the absence of any enforcement mechanism.

---

### Recommendation

1. **Add a penalty mechanism**: Slash a portion of the assigned provider's `accruedFeesInWei` if they fail to fulfill within the exclusivity period, redistributing it to whoever eventually fulfills.
2. **Add a user refund path**: Allow users to cancel and reclaim their fee after a timeout (e.g., `publishTime + exclusivityPeriodSeconds + gracePeriod`) if the request remains unfulfilled.
3. **Use dynamic fee estimation**: Mirror the approach in `apps/fortuna/src/keeper/fee.rs` (`adjust_fee_if_necessary`) at the contract level, or require providers to post a bond that covers fulfillment at current gas prices.

---

### Proof of Concept

1. Gas price is 10 gwei. Provider sets `feePerGasInWei = 10 gwei`. User requests a callback with `callbackGasLimit = 500_000`, paying `5_000_000 gwei = 0.005 ETH` as the gas component of the fee.
2. Gas price spikes to 200 gwei. Actual fulfillment cost: `500_000 × 200 gwei = 0.1 ETH`.
3. Provider's profit from fulfilling: `0.005 ETH - 0.1 ETH = -0.095 ETH`. Provider skips.
4. After `exclusivityPeriodSeconds` (default 15 seconds), any provider may fulfill — but the economics are identical. No provider fulfills.
5. The user's `0.005 ETH` fee is permanently locked. The consumer contract never receives its price update. No revert, no refund, no on-chain recourse. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-84)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-162)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L235-255)
```text
    function getFee(
        address provider,
        uint32 callbackGasLimit,
        bytes32[] calldata priceIds
    ) public view override returns (uint96 feeAmount) {
        uint96 baseFee = _state.pythFeeInWei; // Fixed fee to Pyth
        // Note: The provider needs to set its fees to include the fee charged by the Pyth contract.
        // Ideally, we would be able to automatically compute the pyth fees from the priceIds, but the
        // fee computation on IPyth assumes it has the full updated data.
        uint96 providerBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 providerFeedFee = SafeCast.toUint96(
            priceIds.length * _state.providers[provider].feePerFeedInWei
        );
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L31-46)
```text
    struct ProviderInfo {
        // Slot 1: 12 + 12 + 8 = 32 bytes
        uint96 baseFeeInWei;
        uint96 feePerFeedInWei;
        // 8 bytes padding

        // Slot 2: 12 + 16 + 4 = 32 bytes
        uint96 feePerGasInWei;
        uint128 accruedFeesInWei;
        // 4 bytes padding

        // Slot 3: 20 + 1 + 11 = 32 bytes
        address feeManager;
        bool isRegistered;
        // 11 bytes padding
    }
```
