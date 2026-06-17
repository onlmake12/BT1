### Title
Permanent Loss of User Callback Due to Irreversible Request Clearance Before Callback Execution — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.executeCallback`, the request is permanently cleared and the provider is credited **before** the user callback is invoked. If the callback fails for any reason — including an out-of-gas error caused by insufficient gas forwarding due to EIP-150 — the request cannot be retried and the user's callback is permanently lost with no recourse.

---

### Finding Description

In `Echo.executeCallback`, the execution order is:

1. Validate the request and parse price feeds
2. **Credit the provider** (`accruedFeesInWei += ...`)
3. **Clear the request** (`clearRequest(sequenceNumber)`) — the request is now gone from storage
4. Invoke the user callback with a hard gas cap: `_echoCallback{gas: req.callbackGasLimit}(...)`
5. Catch any failure and emit `PriceUpdateCallbackFailed` [1](#0-0) [2](#0-1) 

The callback is invoked with `gas: req.callbackGasLimit`. Per EIP-150, the EVM can forward **at most 63/64** of the remaining gas in the calling context to a sub-call. If the gas remaining at the point of the `try` block is less than `req.callbackGasLimit * 64/63`, the callback will receive fewer than `callbackGasLimit` gas units and will run out of gas. The `catch` block silently absorbs this failure and emits `PriceUpdateCallbackFailed`. [3](#0-2) 

Because `clearRequest` already ran, the request no longer exists in storage. There is no `CALLBACK_FAILED` state, no retry mechanism, and no refund path. The user's fee has been credited to the provider for a service that was never delivered.

The developers themselves flagged this risk in a TODO comment: [4](#0-3) 

However, the mitigation they applied (try/catch) only prevents `executeCallback` from reverting — it does **not** prevent the permanent loss of the user's callback when the request is already cleared.

Compare this to `Entropy.revealWithCallback`, which has an explicit gas sufficiency check before deciding whether to mark a request as `CALLBACK_FAILED` (retryable) or revert with `InsufficientGas`: [5](#0-4) 

`Echo` has no equivalent protection.

---

### Impact Explanation

A user who requests a price update via `requestPriceUpdatesWithCallback` and pays the required fee will have their callback permanently silenced if `executeCallback` is called with gas that is sufficient to pass all validation and clear the request, but insufficient to forward `callbackGasLimit` gas to the callback. The user's fee is credited to the provider, the request is deleted, and there is no retry path. The user's application logic (e.g., a trade, a settlement, a liquidation) that depended on the callback never executes.

---

### Likelihood Explanation

Any caller of `executeCallback` — including the provider themselves, or any third party after the exclusivity period — can trigger this by submitting the transaction with a gas limit that is between:

- **Lower bound**: enough to pass validation, parse price feeds, credit the provider, and clear the request
- **Upper bound**: less than `callbackGasLimit * 64/63` remaining at the point of the `try` block

This window is practically reachable. The `callbackGasLimit` is a `uint32` set by the user at request time and can be large (e.g., 1,000,000 gas). The overhead before the callback (validation, Pyth parsing, storage writes) is bounded and measurable. A provider acting in bad faith, or a griefing third party after the exclusivity period, can deliberately target this window.

---

### Recommendation

1. **Add a minimum gas check** before the callback, analogous to Entropy's `(startingGas * 31) / 32 > callbackGasLimit` check. If insufficient gas is available, revert the entire transaction so the request is not cleared.
2. **Introduce a retryable failure state** (like Entropy's `CALLBACK_FAILED`) so that if the callback fails after sufficient gas was provided, the user can retry without losing their request.
3. **Move `clearRequest` and fee crediting to after a successful callback**, or only clear on confirmed success/failure with a retry path.

---

### Proof of Concept

```
User calls requestPriceUpdatesWithCallback(callbackGasLimit = 1_000_000)
  → req stored, fee paid

Attacker calls executeCallback{gas: G} where:
  G > (gas needed for validation + parsePriceFeedUpdates + clearRequest + firstUnfulfilledSeq loop)
  G < callbackGasLimit * 64/63 + (above overhead)

Inside executeCallback:
  ✓ findActiveRequest succeeds
  ✓ parsePriceFeedUpdates succeeds
  ✓ provider credited
  ✓ clearRequest(sequenceNumber) — request is GONE
  → try _echoCallback{gas: callbackGasLimit}(...)
      EIP-150: only 63/64 * gasleft() forwarded < callbackGasLimit
      callback OOGs
  → catch: emit PriceUpdateCallbackFailed(...)
  
Result: executeCallback succeeds, provider is paid,
        request is deleted, user callback never fires,
        no retry possible.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-156)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-659)
```text
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
```
