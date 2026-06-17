### Title
Fee Overcharge Based on Desired `callbackGasLimit` Instead of Actual Gas Used — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
In `Echo.sol`, the fee charged to a requester includes `callbackGasLimit * feePerGasInWei`, computed before the callback executes. The actual gas consumed by the callback is always `≤ callbackGasLimit`, yet the provider is credited the full pre-computed gas fee with no refund of the unused portion. This is a direct structural analog of the reported "desired amount vs. actual amount used" fee-accounting bug.

### Finding Description
**Fee calculation at request time** (`getFee`, line 249):
```solidity
uint256 gasFee = callbackGasLimit * providerFeeInWei;
feeAmount = baseFee + providerBaseFee + providerFeedFee + SafeCast.toUint96(gasFee);
```
The entire `callbackGasLimit`-proportional fee is collected upfront and stored as the provider's credit:
```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);   // line 84
```

**Provider credit at execution time** (`executeCallback`, line 161–162):
```solidity
_state.providers[providerToCredit].accruedFeesInWei +=
    SafeCast.toUint128((req.fee + msg.value) - pythFee);
```
The full `req.fee` — which embeds `callbackGasLimit * feePerGasInWei` — is credited to the provider unconditionally.

**Callback execution** (line 177–179):
```solidity
try IEchoConsumer(req.requester)._echoCallback{
    gas: req.callbackGasLimit
}(sequenceNumber, priceFeeds)
```
The callback is forwarded at most `callbackGasLimit` gas, but may consume far less. There is no measurement of `actualGasUsed` and no refund path for `(callbackGasLimit − actualGasUsed) * feePerGasInWei`.

The structural parallel to the reported bug:

| Reported bug | Echo analog |
|---|---|
| Fee charged on `pairedLpDesired` | Fee charged on `callbackGasLimit` |
| Actual amount used = `pairedLpDesired − leftover` | Actual gas used ≤ `callbackGasLimit` |
| Leftover returned to user; fee on it is lost | Unused gas capacity not returned; fee on it is lost |
| Root cause: fee computed before knowing actual usage | Root cause: fee computed before callback executes |

### Impact Explanation
Every requester who sets `callbackGasLimit` conservatively (the recommended practice per the Pyth documentation) overpays by:

```
overpayment = (callbackGasLimit − actualGasUsed) × feePerGasInWei
```

For a callback that uses 100 k gas against a 1 M gas limit at `feePerGasInWei = 1 gwei`, the user overpays by 900 k gwei per request. The excess accrues permanently to the provider's `accruedFeesInWei` balance with no mechanism for the requester to recover it. The IEchoConsumer documentation explicitly states "excess value is *not* refunded," normalising the overcharge.

### Likelihood Explanation
The Pyth developer documentation actively encourages users to add a safety buffer to their gas estimates (e.g., `estimatedGas + safetyBuffer`). This means virtually every production request will have `callbackGasLimit > actualGasUsed`, making the overcharge systematic and continuous rather than edge-case. Any unprivileged user calling `requestPriceUpdatesWithCallback` is affected.

### Recommendation
Measure actual gas consumed inside `executeCallback` and refund the unused gas-fee portion to the requester:

```solidity
uint256 gasStart = gasleft();
try IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...)
{ ... } catch { ... }
uint256 gasUsed = gasStart - gasleft();

uint256 unusedGasFee = (req.callbackGasLimit - gasUsed) *
    _state.providers[req.provider].feePerGasInWei;
// Refund unusedGasFee to req.requester; credit only (req.fee - unusedGasFee) to provider
```

Alternatively, redesign the fee model so `feePerGasInWei` is charged on actual gas used rather than the requested limit.

### Proof of Concept
1. Provider registers with `feePerGasInWei = 1 gwei`.
2. User calls `requestPriceUpdatesWithCallback` with `callbackGasLimit = 1_000_000`, paying `1_000_000 * 1 gwei = 1_000_000 gwei` in gas fees.
3. Provider calls `executeCallback`; the consumer's `_echoCallback` uses 80_000 gas.
4. Provider is credited the full `1_000_000 gwei` gas fee (line 161–162 of `Echo.sol`).
5. User overpaid `920_000 gwei` for gas capacity that was never consumed, with no refund path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L248-254)
```text
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
```
