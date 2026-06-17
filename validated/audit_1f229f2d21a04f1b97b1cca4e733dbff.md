### Title
No Refund for Unused Callback Gas Systematically Overcharges Users and Misaligns Provider Incentives — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol` charges users a gas fee of `callbackGasLimit * feePerGasInWei` at request time. When `executeCallback` runs, the provider is credited the **full pre-paid fee** regardless of how much gas the callback actually consumed. There is no mechanism to refund unused gas to the requester. This directly mirrors the Wormhole NTT finding: users are overcharged with no recourse, and providers face misaligned incentives.

---

### Finding Description

`getFee()` computes the total fee as:

```
feeAmount = pythFeeInWei + baseFeeInWei + (priceIds.length * feePerFeedInWei) + (callbackGasLimit * feePerGasInWei)
``` [1](#0-0) 

The user pays this entire amount upfront in `requestPriceUpdatesWithCallback`. The full `msg.value - pythFeeInWei` is stored in `req.fee`: [2](#0-1) 

In `executeCallback`, the provider is credited the entire stored fee plus any additional `msg.value` sent by the executor, minus only the Pyth oracle fee: [3](#0-2) 

The callback itself is invoked with exactly `req.callbackGasLimit` gas: [4](#0-3) 

At no point is any unused gas refunded to `req.requester`. The `ProviderInfo` struct confirms `accruedFeesInWei` is the only accounting variable — there is no per-request refund path: [5](#0-4) 

A secondary overcharge exists: if `msg.value > getFee(...)` in `requestPriceUpdatesWithCallback`, the surplus is silently absorbed into `req.fee` and later credited to the provider rather than returned to the caller: [6](#0-5) 

---

### Impact Explanation

Every Echo request where the consumer callback uses less gas than `callbackGasLimit` results in the user paying for gas that was never consumed. Because `callbackGasLimit` is a ceiling (users must set it conservatively to avoid out-of-gas failures), the overpayment is structural and occurs on every fulfilled request. Users have no recourse — the contract provides no refund path and no partial-credit mechanism.

---

### Likelihood Explanation

This affects **every** `requestPriceUpdatesWithCallback` call where the callback does not consume exactly `callbackGasLimit` gas, which is virtually all of them in practice. The entry path is fully unprivileged: any user calling `requestPriceUpdatesWithCallback` is affected. No special conditions are required.

---

### Recommendation

In `executeCallback`, measure actual gas consumed by the callback and refund the unused portion to `req.requester`:

```solidity
uint256 gasBefore = gasleft();
try IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...) { ... }
uint256 gasUsed = gasBefore - gasleft();
uint256 unusedGasFee = (req.callbackGasLimit - gasUsed) *
    _state.providers[providerToCredit].feePerGasInWei;
// Refund unusedGasFee to req.requester; credit only actual cost to provider
```

Additionally, refund any `msg.value` surplus above `requiredFee` in `requestPriceUpdatesWithCallback` back to `msg.sender`.

---

### Proof of Concept

1. Provider registers with `feePerGasInWei = 1 gwei`.
2. User calls `requestPriceUpdatesWithCallback` with `callbackGasLimit = 500_000`. Fee charged: `500_000 * 1 gwei = 0.0005 ETH` (plus base fees).
3. Provider calls `executeCallback`. The consumer's `echoCallback` uses only 50,000 gas (10% of the limit).
4. Provider is credited the full `req.fee` — equivalent to 500,000 gas — despite spending only ~50,000 gas on the callback.
5. The 450,000-gas overpayment (~0.00045 ETH at 1 gwei/gas) is permanently transferred to the provider with no refund to the user. [7](#0-6) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-179)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
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
