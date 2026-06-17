### Title
Missing Gas Validation in `executeCallback` Enables Permanent Callback Denial-of-Service - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` forwards a stored `callbackGasLimit` to the consumer's `_echoCallback` via a `try/catch` but performs **no upfront check** that the calling context has enough gas to actually forward that limit. An unprivileged caller can invoke `executeCallback` with deliberately insufficient gas, causing the callback to silently fail via the `catch` branch while the request is **permanently cleared** and the provider is **permanently credited**. The consumer's application logic is irreversibly skipped with no retry path.

---

### Finding Description

`Echo.executeCallback` is a permissionless function (callable by anyone after the exclusivity window). Its execution flow is:

1. Validates price IDs and parses price feeds.
2. Credits the provider: `_state.providers[providerToCredit].accruedFeesInWei += ...`
3. **Permanently clears the request**: `clearRequest(sequenceNumber)`
4. Invokes the consumer callback with the stored gas limit via `try/catch`. [1](#0-0) [2](#0-1) 

The critical problem: there is **no `require(gasleft() >= req.callbackGasLimit, ...)`** guard before the `try` block. Due to EVM's 63/64 rule, if the outer transaction provides gas just barely sufficient to reach the `try` statement but not enough to forward `req.callbackGasLimit` to the sub-call, the callback receives less gas than requested, runs out of gas, and the `catch` branch fires:

```solidity
} catch {
    emit PriceUpdateCallbackFailed(..., "low-level error (possibly out of gas)");
}
```

At this point the request slot is already cleared and the provider already credited — there is no revert, no retry mechanism, and no refund to the consumer.

The NatSpec on `IEcho.executeCallback` acknowledges a 1.5× gas overhead requirement but **does not enforce it in code**: [3](#0-2) 

Compare this to `Entropy.revealWithCallback`, which explicitly reverts with `InsufficientGas` when the outer context cannot supply the required gas, preserving the request for retry: [4](#0-3) 

`Echo.sol` has no equivalent protection.

---

### Impact Explanation

- The consumer's `echoCallback` logic (e.g., price-triggered trade execution, state update, liquidation) is **permanently and silently skipped** for the targeted sequence number.
- The request is **irrecoverably consumed**: `clearRequest` runs before the callback, so there is no re-execution path.
- The consumer already paid the fee at request time; they receive no refund and no callback.
- This constitutes a **permanent, targeted denial of service** against any specific pending Echo request. [5](#0-4) 

---

### Likelihood Explanation

- `executeCallback` is **permissionless** — any EOA or contract can call it for any pending sequence number.
- After the exclusivity period, even the original provider restriction is lifted, making the attack window fully open.
- The attacker only needs to submit the transaction with a gas limit calibrated to exhaust gas inside the callback sub-call. This is straightforward to compute off-chain from the stored `callbackGasLimit`.
- No special privileges, leaked keys, or governance access are required. [6](#0-5) 

---

### Recommendation

Add an upfront gas sufficiency check at the start of `executeCallback`, mirroring the pattern used in `Entropy.revealWithCallback`:

```solidity
// Ensure the outer context can forward at least req.callbackGasLimit to the sub-call.
// The 63/64 rule means we need more than callbackGasLimit available here.
// Using 31/32 as a margin of safety (same as Entropy.sol).
require(
    gasleft() * 31 / 32 >= req.callbackGasLimit,
    "Insufficient gas to execute callback"
);
```

This should be placed **before** `clearRequest` and the provider credit, or alternatively the request should only be cleared after a successful callback (with a re-entrancy guard). The check ensures that if the caller provides insufficient gas, the entire transaction reverts and the request remains active for a legitimate retry. [7](#0-6) 

---

### Proof of Concept

```solidity
// Attacker targets sequenceNumber N which has callbackGasLimit = 1_000_000
// Attacker calls executeCallback with ~200_000 gas total.
// The function passes all pre-callback checks (price ID validation, fee accounting, clearRequest)
// consuming ~150_000 gas, leaving ~50_000 for the try block.
// The sub-call receives at most 63/64 * 50_000 ≈ 49_218 gas — far below 1_000_000.
// The callback OOGs, the catch branch emits PriceUpdateCallbackFailed.
// Request is cleared. Provider is credited. Consumer callback never ran.

echo.executeCallback{gas: 200_000}(
    providerToCredit,
    sequenceNumber,   // victim's request
    updateData,
    priceIds
);
// Result: PriceUpdateCallbackFailed emitted, request permanently gone.
```

The existing test `testExecuteCallbackWithInsufficientGas` in `Echo.t.sol` confirms the function reverts at the OOG boundary but does **not** test the scenario where gas is sufficient to clear the request but insufficient for the callback — the silent-failure path through `catch`. [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-164)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L62-65)
```text
     * @notice Executes the callback for a price update request
     * @dev Requires 1.5x the callback gas limit to account for cross-contract call overhead
     * For example, if callbackGasLimit is 1M, the transaction needs at least 1.5M gas + some gas for some other operations in the function before the callback
     * @param providerToCredit The provider to credit for fulfilling the request. This may not be the provider that submitted the request (if the exclusivity period has elapsed).
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-660)
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
            }
```

**File:** target_chains/ethereum/contracts/test/Echo.t.sol (L382-406)
```text
    function testExecuteCallbackWithInsufficientGas() public {
        // Setup request with 1M gas limit
        (
            uint64 sequenceNumber,
            bytes32[] memory priceIds,
            uint256 publishTime
        ) = setupConsumerRequest(echo, defaultProvider, address(consumer));

        // Setup mock data
        PythStructs.PriceFeed[] memory priceFeeds = createMockPriceFeeds(
            publishTime
        );
        mockParsePriceFeedUpdates(pyth, priceFeeds);
        bytes[] memory updateData = createMockUpdateData(priceFeeds);

        // Try executing with only 100K gas when 1M is required
        vm.prank(defaultProvider);
        vm.expectRevert(); // Just expect any revert since it will be an out-of-gas error
        echo.executeCallback{gas: 100000}(
            defaultProvider,
            sequenceNumber,
            updateData,
            priceIds
        ); // Will fail because gasleft() < callbackGasLimit
    }
```
