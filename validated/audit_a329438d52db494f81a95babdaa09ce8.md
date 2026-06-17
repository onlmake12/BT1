### Title
Excess ETH Overpayment Not Refunded to Caller in `requestPriceUpdatesWithCallback` — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.requestPriceUpdatesWithCallback` accepts any `msg.value >= requiredFee` but silently absorbs the excess into the provider's accrued fee balance instead of refunding it to the caller. Any user who overpays permanently loses the excess ETH.

---

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` enforces only a minimum fee check:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [1](#0-0) 

Immediately after, the entire `msg.value` minus only the fixed `pythFeeInWei` is stored as the provider's fee:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [2](#0-1) 

When `executeCallback` is later called, the provider is credited:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

If a user sends `msg.value = requiredFee + X` (any positive `X`), the excess `X` is permanently credited to the provider's `accruedFeesInWei` and is never returned to the caller. There is no refund path anywhere in the function.

The `IEcho` interface NatSpec states `msg.value must be equal to getFee(callbackGasLimit)`, but the implementation only enforces a lower bound, creating a silent fund-loss vector for any overpayment. [4](#0-3) 

---

### Impact Explanation

Any ETH sent above `getFee(provider, callbackGasLimit, priceIds)` is permanently transferred to the provider's accrued fee balance. The original caller has no mechanism to recover it. This is a direct, irreversible loss of user funds with no protocol benefit.

**Impact: Medium** — funds are lost, but only the excess above the required fee is affected per call.

---

### Likelihood Explanation

Several realistic scenarios cause overpayment:

1. **Fee race condition**: A user queries `getFee()` off-chain, then the provider calls `setProviderFee()` to lower fees before the user's transaction is mined. The user's transaction succeeds with the old (higher) value, and the excess is absorbed.
2. **Round-number padding**: Users or integrating contracts commonly send a small buffer (e.g., `fee * 110 / 100`) to avoid reverts from fee fluctuations.
3. **Stale fee estimate**: Off-chain fee estimation at a different gas price than execution.

Provider fee updates are permissionless for registered providers:

```solidity
function setProviderFee(
    address provider,
    uint96 newBaseFeeInWei,
    ...
) external override {
``` [5](#0-4) 

**Likelihood: Medium** — fee changes and buffer-sending are common in production usage.

---

### Recommendation

Add a refund of excess ETH at the end of `requestPriceUpdatesWithCallback`, analogous to how `PythLazer.verifyUpdate` handles it:

```solidity
// In PythLazer.sol — correct pattern
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [6](#0-5) 

Apply the same pattern in `Echo.requestPriceUpdatesWithCallback`:

```solidity
if (msg.value > requiredFee) {
    (bool ok, ) = payable(msg.sender).call{value: msg.value - requiredFee}("");
    require(ok, "Refund failed");
}
```

Alternatively, enforce strict equality: `if (msg.value != requiredFee) revert InvalidFee();`.

---

### Proof of Concept

1. Provider registers with `baseFeeInWei = 1000 wei`, `feePerFeedInWei = 100 wei`, `feePerGasInWei = 1 wei`.
2. User queries `getFee(provider, 100000, priceIds)` → returns `requiredFee = R`.
3. Provider calls `setProviderFee` to reduce fees (or user simply sends `R + 1000 wei` as a buffer).
4. User calls `requestPriceUpdatesWithCallback{value: R + 1000}(...)`.
5. Check passes (`msg.value >= requiredFee`).
6. `req.fee = (R + 1000) - pythFeeInWei` — the 1000 wei excess is baked into the provider's fee.
7. After `executeCallback`, provider's `accruedFeesInWei` includes the extra 1000 wei.
8. User has permanently lost 1000 wei with no recourse. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-401)
```text
    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L41-41)
```text
     * @dev The msg.value must be equal to getFee(callbackGasLimit)
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L75-77)
```text
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
