### Title
Excess ETH Overpayment Not Refunded to Caller in `requestPriceUpdatesWithCallback` — (`Echo.sol`)

### Summary

`Echo.requestPriceUpdatesWithCallback` accepts `msg.value >= requiredFee` but never refunds the difference when a caller overpays. The entire surplus is silently stored in `req.fee` and later credited to the provider's accrued balance in `executeCallback`, permanently transferring the user's excess ETH to the provider.

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` computes a `requiredFee` and enforces a minimum:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [1](#0-0) 

It then stores the provider's portion of the fee as:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [2](#0-1) 

When `msg.value > requiredFee`, the overpayment `(msg.value - requiredFee)` is silently folded into `req.fee`. There is no refund path. Later, in `executeCallback`, the full `req.fee` (including the surplus) is credited to the provider:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

The user's excess ETH is never returned; it flows directly into the provider's withdrawable balance.

This is structurally identical to the H-05 pattern: a caller sends a maximum/excess amount, only part of it is consumed, and the remainder is not returned.

### Impact Explanation

Any caller of `requestPriceUpdatesWithCallback` who sends `msg.value` greater than `getFee(provider, callbackGasLimit, priceIds)` permanently loses the excess ETH. The surplus is credited to the provider, not the user. This is a direct, quantifiable loss of user funds with no recovery mechanism.

### Likelihood Explanation

Overpayment is a common and realistic scenario:
- Callers often add a buffer to avoid `InsufficientFee` reverts when fees fluctuate.
- Integrators may hardcode a conservative `msg.value` above the computed fee.
- Any frontend that adds a small safety margin will silently cause users to overpay.

The entry path requires no privilege: any EOA or contract calling `requestPriceUpdatesWithCallback` with excess ETH triggers the loss.

### Recommendation

Compute the exact required fee, use only that amount, and refund the remainder before storing `req.fee`:

```diff
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();

+if (msg.value > requiredFee) {
+    (bool refunded, ) = payable(msg.sender).call{value: msg.value - requiredFee}("");
+    require(refunded, "Refund failed");
+}

 req.fee = SafeCast.toUint96(requiredFee - _state.pythFeeInWei);
```

### Proof of Concept

1. Deploy `Echo` with a registered provider whose `getFee` returns `X` wei.
2. Call `requestPriceUpdatesWithCallback{value: X + 1 ether}(...)`.
3. Record the caller's ETH balance before and after.
4. Observe that the caller's balance decreased by `X + 1 ether` (not `X`).
5. After `executeCallback` is called, observe that the provider's `accruedFeesInWei` includes the extra `1 ether`.
6. The caller has permanently lost `1 ether` with no recourse.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-76)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-84)
```text
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```
