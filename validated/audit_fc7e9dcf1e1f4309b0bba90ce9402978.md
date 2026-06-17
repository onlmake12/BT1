### Title
Excess ETH Overpayment Silently Credited to Provider Instead of Refunded to User - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

In `Echo.sol`, the `requestPriceUpdatesWithCallback` function stores the entire `msg.value - pythFeeInWei` as `req.fee` (the provider fee). When a user overpays relative to the actual required fee, the excess is not refunded — it is silently credited to the provider upon `executeCallback`. There is no upper-bound enforcement or refund mechanism.

### Finding Description

In `requestPriceUpdatesWithCallback`, the required fee is computed and a lower-bound check is enforced:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [1](#0-0) 

However, the stored provider fee is set to the **full** `msg.value - pythFeeInWei`, not the required provider portion:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [2](#0-1) 

When `executeCallback` is later called, this entire `req.fee` (including any overpayment) is credited to the provider:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

There is no refund path. The `IEcho` interface comment explicitly states `"The msg.value must be equal to getFee(callbackGasLimit)"`, acknowledging exact payment is expected — but the contract only enforces the lower bound, not the upper bound. [4](#0-3) 

The provider fee structure is dynamic and can be changed at any time by the provider or fee manager via `setProviderFee`: [5](#0-4) 

### Impact Explanation

Any user who sends `msg.value > requiredFee` (e.g., as a buffer against fee changes, or due to a fee decrease between estimation and execution) permanently loses the excess ETH to the provider. The excess is not stuck in the contract — it is transferred to the provider's accrued balance, making it unrecoverable by the user. This breaks the expected user experience where overpayment should be refunded.

### Likelihood Explanation

Realistic. Provider fees can change between the time a user calls `getFee()` and the time they submit `requestPriceUpdatesWithCallback`. Users who add a small ETH buffer to avoid reverts (a common pattern) will silently lose that buffer to the provider. The `IEntropyV2` interface explicitly documents "excess value is *not* refunded" as a known behavior for Entropy, but `IEcho` does not — it says `msg.value` **must equal** the fee, implying refund is expected. [6](#0-5) 

### Recommendation

Replace the stored fee with the required provider portion only, and refund any excess to the caller:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();

// Store only the required provider fee, not the full msg.value
req.fee = requiredFee - _state.pythFeeInWei;
_state.accruedFeesInWei += _state.pythFeeInWei;

// Refund excess to caller
if (msg.value > requiredFee) {
    (bool sent, ) = payable(msg.sender).call{value: msg.value - requiredFee}("");
    require(sent, "Refund failed");
}
```

### Proof of Concept

1. Provider registers with `baseFeeInWei = 100`, `feePerFeedInWei = 50`, `feePerGasInWei = 1`. `pythFeeInWei = 10`.
2. User calls `getFee(provider, 1000, [priceId])` → returns `10 + 100 + 50 + 1000 = 1160 wei`.
3. Provider calls `setProviderFee` reducing `baseFeeInWei` to `50` in the same block.
4. User submits `requestPriceUpdatesWithCallback{value: 1160}(...)`. New `requiredFee = 1110`. `msg.value (1160) >= 1110`, so no revert.
5. `req.fee = 1160 - 10 = 1150` is stored (should be `1100`).
6. Provider calls `executeCallback`. `accruedFeesInWei += 1150 - pythFee_at_execution`. The user's 50 wei overpayment is permanently credited to the provider with no refund. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-102)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );

        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
        require(publishTime <= block.timestamp + 60, "Too far in future");
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
        requestSequenceNumber = _state.currentSequenceNumber++;

        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
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
        _state.accruedFeesInWei += _state.pythFeeInWei;

        emit PriceUpdateRequested(req, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-426)
```text
    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
        require(
            msg.sender == provider ||
                msg.sender == _state.providers[provider].feeManager,
            "Only provider or fee manager can invoke this method"
        );

        uint96 oldBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 oldFeePerFeed = _state.providers[provider].feePerFeedInWei;
        uint96 oldFeePerGas = _state.providers[provider].feePerGasInWei;
        _state.providers[provider].baseFeeInWei = newBaseFeeInWei;
        _state.providers[provider].feePerFeedInWei = newFeePerFeedInWei;
        _state.providers[provider].feePerGasInWei = newFeePerGasInWei;
        emit ProviderFeeUpdated(
            provider,
            oldBaseFee,
            oldFeePerFeed,
            oldFeePerGas,
            newBaseFeeInWei,
            newFeePerFeedInWei,
            newFeePerGasInWei
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L41-41)
```text
     * @dev The msg.value must be equal to getFee(callbackGasLimit)
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L18-19)
```text
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
