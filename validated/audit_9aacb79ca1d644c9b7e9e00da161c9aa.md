### Title
Entropy/Echo Provider Can Instantly Raise Fees Without Timelock, Enabling Frontrunning to Drain User ETH Overpayments — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

Both `Entropy.sol` and `Echo.sol` allow any permissionlessly registered provider to instantly change their fees to an arbitrary value with no timelock, no delay, and no maximum cap. In `Echo.sol`, the entire `msg.value - pythFee` is stored as the provider's fee in the request, meaning any ETH a user sends above the minimum required fee is captured by the provider. A malicious provider can frontrun a user's pending `requestPriceUpdatesWithCallback` transaction by raising fees to exactly the user's `msg.value`, extracting the user's ETH buffer as profit.

---

### Finding Description

**Root cause — `Echo.sol` `setProviderFee()`:**

`setProviderFee()` in `Echo.sol` allows the provider (or their fee manager) to instantly update all three fee parameters to any `uint96` value with no timelock, no delay, and no cap: [1](#0-0) 

Provider registration is fully permissionless: [2](#0-1) 

**Fee storage in `requestPriceUpdatesWithCallback`:**

When a user submits a request, the contract stores `msg.value - pythFee` as the provider's fee — capturing **all** ETH the user sent beyond the Pyth protocol fee: [3](#0-2) 

This means if a user sends excess ETH (a common defensive pattern to avoid reverts from fee changes), the entire excess is credited to the provider when `executeCallback` is called.

**Analogous root cause in `Entropy.sol`:**

`setProviderFee()` in `Entropy.sol` has the same absence of timelock or cap: [4](#0-3) 

In Entropy, however, excess ETH goes to `_state.accruedPythFeesInWei` (Pyth protocol), not the provider: [5](#0-4) 

So in Entropy the impact is limited to DoS (user transactions revert when the provider sets fees above `msg.value`). In Echo the impact is direct ETH extraction.

**The `getFee()` formula in Echo** depends on all three provider-controlled parameters: [6](#0-5) 

All three can be changed atomically in a single `setProviderFee()` call, allowing the provider to target any specific `msg.value`.

---

### Impact Explanation

**Echo (higher impact):** A malicious provider can frontrun a user's `requestPriceUpdatesWithCallback` transaction by calling `setProviderFee()` to set fees to exactly `msg.value - pythFee`. The user's transaction succeeds (fee check passes), but `req.fee` captures the full excess. When the provider calls `executeCallback`, they receive the inflated fee. Users who send any ETH buffer above the quoted fee lose that buffer to the provider.

**Entropy (lower impact):** A malicious provider can set `feeInWei` to `type(uint128).max`, causing all pending user requests to revert with `InsufficientFee`. Users lose gas but not ETH.

In both cases, the interface explicitly warns that excess value is not refunded: [7](#0-6) 

---

### Likelihood Explanation

- Provider registration is permissionless — anyone can call `registerProvider()` in Echo or `register()` in Entropy.
- Fee changes take effect in the same block with no delay.
- No maximum fee cap exists in either contract.
- Frontrunning is feasible on all EVM chains with public mempools.
- Many user contracts and SDKs add a fee buffer (e.g., `getFee() * 2`) to avoid reverts from fee fluctuations — this is the exact ETH the attacker captures.
- The official SDK documentation explicitly warns that fees can change over time and instructs callers to compute fees on-chain before each request, implying users routinely send slightly more than the minimum. [8](#0-7) 

---

### Recommendation

1. **Add a timelock to fee changes** — require a minimum delay (e.g., 24–48 hours) between a fee-change announcement and its activation, so users can observe the pending change and avoid the provider.
2. **Add a maximum fee cap** — bound `feeInWei` / `baseFeeInWei` / `feePerFeedInWei` / `feePerGasInWei` to a protocol-defined maximum.
3. **Refund excess ETH to the user in Echo** — instead of `req.fee = msg.value - pythFee`, store only `requiredFee - pythFee` and refund `msg.value - requiredFee` to `msg.sender`. This eliminates the extraction vector entirely.

---

### Proof of Concept

```solidity
// Setup: malicious provider registers with low fees
vm.prank(maliciousProvider);
echo.registerProvider(1 wei, 1 wei, 1 wei);

// User queries fee off-chain: quotedFee = 1001 wei (example)
uint96 quotedFee = echo.getFee(maliciousProvider, gasLimit, priceIds);
// User adds 2x buffer for safety
uint96 userPayment = quotedFee * 2; // = 2002 wei

// User's requestPriceUpdatesWithCallback{value: 2002} is now pending in mempool.
// Malicious provider sees it and frontruns:

vm.prank(maliciousProvider);
// Set baseFee so that getFee() == userPayment == 2002 wei
echo.setProviderFee(
    maliciousProvider,
    uint96(userPayment - PYTH_FEE),  // baseFee captures all user ETH
    0,
    0
);

// User's transaction now executes:
vm.prank(user);
echo.requestPriceUpdatesWithCallback{value: userPayment}(
    maliciousProvider, publishTime, priceIds, gasLimit
);
// req.fee = userPayment - PYTH_FEE  (provider gets 2x the original fee)

// Provider calls executeCallback → receives req.fee = 2001 wei
// instead of the original 1000 wei.
// Provider extracted 1001 wei (the user's buffer) as profit.
```

The same DoS variant applies to Entropy: the provider calls `setProviderFee(type(uint128).max)` before the user's `requestV2` lands, causing it to revert with `InsufficientFee`. [9](#0-8)

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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L42-44)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(gasLimit)`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2(gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
