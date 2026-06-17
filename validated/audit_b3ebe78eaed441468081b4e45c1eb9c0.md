### Title
No Maximum Fee Guard Allows Provider to Frontrun and Drain User's Excess ETH Payment — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `requestHelper` function in `Entropy.sol` and `requestPriceUpdatesWithCallback` in `Echo.sol` accept `msg.value >= requiredFee` but **never refund excess ETH** to the caller, and neither function accepts a `maxFee` parameter. Because providers can call `setProviderFee` / `setProviderFeeAsFeeManager` at any time with no timelock, a registered provider can observe a user's pending transaction in the mempool, frontrun it with a fee increase calibrated to the user's `msg.value`, and capture the user's buffer payment.

---

### Finding Description

**Root cause — no refund of excess `msg.value`:**

In `Entropy.sol` `requestHelper`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
```

Every wei above `providerFee` is silently credited to `_state.accruedPythFeesInWei`. The interface documentation explicitly acknowledges this: *"excess value is not refunded to the caller."* [1](#0-0) 

The fee accounting in `requestHelper` confirms the no-refund behavior: [2](#0-1) 

**Root cause — provider can change fee at any time:**

`setProviderFee` has no timelock or delay: [3](#0-2) 

**Same pattern in Echo:**

`requestPriceUpdatesWithCallback` stores `req.fee = msg.value - pythFeeInWei`, meaning any overpayment is locked into the request and later credited entirely to the provider on `executeCallback`: [4](#0-3) 

The provider fee in Echo is also freely updatable: [5](#0-4) 

---

### Impact Explanation

A user who sends `msg.value = quotedFee + buffer` (a common defensive pattern to avoid reverts from fee volatility) can have their entire buffer stolen by a frontrunning provider. In Entropy, the provider captures the delta between the new `providerFee` and the old one; in Echo, the provider captures the full `req.fee` surplus when they execute the callback. The user receives the same service (a sequence number / price update) but pays materially more than the on-chain quoted price at the time they composed the transaction — with no recourse and no revert to signal the overcharge.

---

### Likelihood Explanation

- Any address can register as a provider (`register` is permissionless). [6](#0-5) 
- EVM mempools are public on all chains where Entropy is deployed; pending transactions and their `msg.value` are visible before inclusion.
- The provider has a direct financial incentive: calling `setProviderFee(msg.value - pythFeeInWei)` maximises their `accruedFeesInWei` capture from a single user transaction.
- No timelock, no delay, no governance approval is required to change the fee.

---

### Recommendation

1. **Refund excess `msg.value`**: After crediting `providerFee` and `pythFee`, return `msg.value - requiredFee` to `msg.sender`.
2. **Accept a `maxFee` parameter**: Allow callers to pass the maximum fee they are willing to pay; revert if `requiredFee > maxFee`. This is the direct analog of the minimum-return check recommended in M-01.

---

### Proof of Concept

**Entropy scenario:**

1. Provider registers with `feeInWei = 50 wei`; `pythFeeInWei = 100 wei`.
2. User calls `getFeeV2()` off-chain → returns `150 wei`.
3. User submits `requestV2{value: 200 wei}()` (50 wei buffer to avoid revert risk).
4. Provider sees the pending tx in the mempool; calls `setProviderFee(100 wei)` with higher gas to frontrun.
5. Provider's tx lands first; `requiredFee` is now `200 wei`.
6. User's tx executes: `msg.value (200) >= requiredFee (200)` → succeeds.
7. `providerInfo.accruedFeesInWei += 100` (provider captured 50 extra wei).
8. `_state.accruedPythFeesInWei += 100` (Pyth gets 100 instead of 150).
9. User paid `200 wei` for a service quoted at `150 wei`; lost `50 wei` with no warning or revert.

**Echo scenario (provider profits more directly):**

1. Provider registers; user queries `getFee()` = `150 wei`.
2. User submits `requestPriceUpdatesWithCallback{value: 200 wei}(...)`.
3. Provider frontruns with `setProviderFee(...)` to raise their fee.
4. `req.fee = 200 - 100 = 100 wei` (instead of `50 wei`) is stored.
5. When provider calls `executeCallback`, they receive `req.fee + msg.value - pythFee = 100 + 0 - 0 = 100 wei` instead of `50 wei`. [7](#0-6)

### Citations

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-145)
```text
    function register(
        uint128 feeInWei,
        bytes32 commitment,
        bytes calldata commitmentMetadata,
        uint64 chainLength,
        bytes calldata uri
    ) public override {
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();

        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        // NOTE: this method implementation depends on the fact that ProviderInfo will be initialized to all-zero.
        // Specifically, accruedFeesInWei is intentionally not set. On initial registration, it will be zero,
        // then on future registrations, it will be unchanged. Similarly, provider.sequenceNumber defaults to 0
        // on initial registration.

        provider.feeInWei = feeInWei;

        provider.originalCommitment = commitment;
        provider.originalCommitmentSequenceNumber = provider.sequenceNumber;
        provider.currentCommitment = commitment;
        provider.currentCommitmentSequenceNumber = provider.sequenceNumber;
        provider.commitmentMetadata = commitmentMetadata;
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
        provider.uri = uri;

        provider.sequenceNumber += 1;

        emit EntropyEvents.Registered(
            EntropyStructConverter.toV1ProviderInfo(provider)
        );
        emit EntropyEventsV2.Registered(msg.sender, bytes(""));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-827)
```text
    function setProviderFee(uint128 newFeeInWei) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
        uint128 oldFeeInWei = provider.feeInWei;
        provider.feeInWei = newFeeInWei;
        emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
        emit EntropyEventsV2.ProviderFeeUpdated(
            msg.sender,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

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
