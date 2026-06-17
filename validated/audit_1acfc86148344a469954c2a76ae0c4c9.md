### Title
Attacker Can Permanently Destroy User Callbacks via Insufficient Gas in `executeCallback` — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` clears the user's request and credits the provider **before** invoking the user's callback via `try/catch`. Because the `try` block forwards exactly `req.callbackGasLimit` gas but the EVM's 63/64 rule means the actual gas forwarded is `min(callbackGasLimit, 63/64 * gasleft())`, an attacker can call `executeCallback` with a carefully chosen total gas limit that is sufficient for all pre-callback logic but leaves the callback with less than `callbackGasLimit` gas. The callback fails with out-of-gas, the `catch` block silently emits `PriceUpdateCallbackFailed`, and the request is already cleared — permanently and irrecoverably lost.

---

### Finding Description

In `Echo.sol`, `executeCallback` is callable by anyone after the exclusivity period expires. The function's execution order is:

1. Validate request and price IDs
2. Parse Pyth price feeds
3. **Credit provider fees** (line 161–162)
4. **Clear the request** (line 164)
5. Advance `firstUnfulfilledSeq` (lines 169–174)
6. **Invoke callback** via `try/catch` with `gas: req.callbackGasLimit` (lines 176–201) [1](#0-0) [2](#0-1) 

The `try` block passes `gas: req.callbackGasLimit` as an explicit gas stipend. Per EVM semantics (EIP-150 / 63/64 rule), the actual gas forwarded to the callee is `min(callbackGasLimit, 63/64 * gasleft())`. If `gasleft()` at the point of the call is less than `callbackGasLimit * 64/63`, the callback receives fewer than `callbackGasLimit` gas units and may run out of gas mid-execution.

The `catch` block (line 192–200) silently catches this out-of-gas error and emits `PriceUpdateCallbackFailed` with the message `"low-level error (possibly out of gas)"`. The transaction does **not** revert. Because `clearRequest` was already called at line 164, the request slot is gone — there is no retry path. [3](#0-2) 

By contrast, Pyth's `Entropy.revealWithCallback` explicitly guards against this with a gas sufficiency check before treating a callback failure as legitimate:

```solidity
} else if (
    (startingGas * 31) / 32 >
    uint256(req.gasLimit10k) * TEN_THOUSAND
) {
``` [4](#0-3) 

If this check fails, `Entropy` reverts with `InsufficientGas`, preserving the request. Echo has no equivalent check. [5](#0-4) 

---

### Impact Explanation

- The user's `_echoCallback` is permanently never executed. The request is cleared with no recovery mechanism.
- The user's fee is fully consumed (provider is credited at line 161–162 before the callback).
- The user receives no price update and no refund.
- The attacker pays only the gas cost of the `executeCallback` transaction. [1](#0-0) 

---

### Likelihood Explanation

- `executeCallback` has no caller access control after the exclusivity period. Any address can call it.
- The attacker only needs to choose a transaction `gasLimit` in the range `[gas_for_pre_callback_logic + callbackGasLimit, gas_for_pre_callback_logic + callbackGasLimit * 64/63)`. This range is non-empty and straightforward to target by simulating the transaction.
- The attack is permissionless, requires no privileged role, and is repeatable for any pending Echo request.
- The exclusivity period only delays the attack window; it does not prevent it. [6](#0-5) 

---

### Recommendation

Add a gas sufficiency check before the `try` block, mirroring the pattern in `Entropy.revealWithCallback`:

```solidity
uint256 startingGas = gasleft();
// Ensure the calling context has enough gas to forward callbackGasLimit to the callback.
// We use 31/32 (< 63/64) as a conservative safety margin.
if ((startingGas * 31) / 32 <= req.callbackGasLimit) {
    revert InsufficientGas();
}
```

This ensures that if the callback fails, it genuinely ran out of gas within the callback itself (not due to the caller providing insufficient outer gas), and the revert preserves the request for retry.

Additionally, consider not clearing the request before the callback, or implementing a retry/recovery mechanism analogous to Entropy's `CALLBACK_FAILED` state.

---

### Proof of Concept

```
Setup:
  - User calls requestPriceUpdatesWithCallback(..., callbackGasLimit = 500_000)
  - Request is stored on-chain

Attack (after exclusivity period):
  - Attacker estimates gas cost of pre-callback logic in executeCallback ≈ G_pre (e.g., ~150_000)
  - Attacker calls executeCallback{gas: G_pre + 500_000 * 64/63 - 1}(...)
    (i.e., total gas just below what's needed to forward the full 500_000 to the callback)

Execution trace:
  1. Pre-callback logic succeeds (G_pre gas consumed)
  2. gasleft() ≈ 500_000 * 64/63 - 1 at the try call site
  3. EVM forwards min(500_000, 63/64 * gasleft()) = 63/64 * (500_000 * 64/63 - 1) < 500_000 gas
  4. Callback runs out of gas
  5. catch block fires → PriceUpdateCallbackFailed emitted
  6. Request already cleared at step (clearRequest) → permanently lost
  7. Provider already credited → user's fee gone

Result: User's callback is permanently destroyed. User loses fee. No recovery possible.
```

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-164)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-201)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
        {
            // Callback succeeded
            emitPriceUpdate(sequenceNumber, priceIds, priceFeeds);
        } catch Error(string memory reason) {
            // Explicit revert/require
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                reason
            );
        } catch {
            // Out of gas or other low-level errors
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                "low-level error (possibly out of gas)"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-624)
```text
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L652-659)
```text
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
```
