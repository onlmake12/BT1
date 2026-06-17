### Title
`Echo.executeCallback()` Permanently Loses User Callback With No Retry Mechanism When `_echoCallback` Fails - (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback()` credits the provider fee and clears the request **before** attempting the consumer callback. If the callback permanently fails (caught by `try/catch`), the request is irrecoverably gone with no retry path — unlike `Entropy.sol` which has a `CALLBACK_FAILED` state. The user's fee is consumed and the price-update callback is permanently undeliverable.

---

### Finding Description

In `Echo.executeCallback()`, the execution order is:

1. **Credit provider fee** and **clear the request** (effects applied first): [1](#0-0) 

2. **Attempt the callback** with a `try/catch` that silently swallows failures: [2](#0-1) 

When the `_echoCallback` on the requester contract fails (e.g., the requester contract always reverts, has been self-destructed, or has a permanent logic bug), the `catch` branch emits `PriceUpdateCallbackFailed` and returns. The request has already been cleared by `clearRequest(sequenceNumber)` and the fee has already been credited to the provider. There is **no `CALLBACK_FAILED` state**, no retry entry point, and no refund path.

The developer explicitly acknowledged the related risk in a TODO comment: [3](#0-2) 

By contrast, `Entropy.sol` handles this correctly: when a callback fails, the request is moved to `CALLBACK_FAILED` state and left active so that `revealWithCallback()` can be called again: [4](#0-3) 

Echo has no equivalent recovery state.

---

### Impact Explanation

**High.** A user who calls `requestPriceUpdatesWithCallback()` and pays the full fee (provider base fee + per-feed fee + gas fee) receives no price-update callback if the callback permanently fails. The fee is irrecoverably credited to the provider. The user cannot retry, cannot cancel, and cannot obtain a refund. The price-update service is paid for but never delivered, with no on-chain recourse.

---

### Likelihood Explanation

**Medium.** Permanent callback failures occur when:
- The requester contract has a logic bug that always reverts in `_echoCallback`.
- The requester contract is self-destructed between request and fulfillment.
- The requester contract runs out of gas unconditionally (e.g., unbounded loop in callback).
- A malicious or misconfigured consumer contract is deployed.

These are realistic scenarios for any non-trivial consumer contract. The `callbackGasLimit` is set by the user at request time; if it is set too low for the actual callback logic, every fulfillment attempt will silently fail with no retry.

---

### Recommendation

Implement a `CALLBACK_FAILED` state analogous to `Entropy.sol`:

- Do **not** call `clearRequest()` before the callback attempt.
- If the callback fails, set `req.callbackStatus = CALLBACK_FAILED` and keep the request active.
- Expose a retry entry point (e.g., `retryCallback()`) that re-attempts delivery.
- Alternatively, if no retry is desired, refund `req.fee` to `req.requester` when the callback permanently fails, rather than crediting it to the provider.

---

### Proof of Concept

1. Deploy a `FailingEchoConsumer` whose `_echoCallback` always reverts (this contract already exists in the test suite): [5](#0-4) 

2. Call `requestPriceUpdatesWithCallback()` from `FailingEchoConsumer`, paying the full fee. The fee is split: `_state.accruedFeesInWei += pythFeeInWei` and `req.fee = msg.value - pythFeeInWei` stored for the provider. [6](#0-5) 

3. Provider calls `executeCallback()`. Provider fee is credited and request is cleared **before** the callback: [1](#0-0) 

4. The `try` block calls `_echoCallback` which reverts. The `catch` block emits `PriceUpdateCallbackFailed` and returns. The test confirms this behavior: [7](#0-6) 

5. The request is gone. `findActiveRequest(sequenceNumber)` now reverts with `NoSuchRequest`. There is no retry function. The user's fee is permanently consumed with no callback delivered and no refund path.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-99)
```text
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
```

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-651)
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
```

**File:** target_chains/ethereum/contracts/test/Echo.t.sol (L46-63)
```text
contract FailingEchoConsumer is IEchoConsumer {
    address private _echo;

    constructor(address echo) {
        _echo = echo;
    }

    function getEcho() internal view override returns (address) {
        return _echo;
    }

    function echoCallback(
        uint64,
        PythStructs.PriceFeed[] memory
    ) internal pure override {
        revert("callback failed");
    }
}
```

**File:** target_chains/ethereum/contracts/test/Echo.t.sol (L304-341)
```text
    function testExecuteCallbackFailure() public {
        FailingEchoConsumer failingConsumer = new FailingEchoConsumer(
            address(proxy)
        );

        (
            uint64 sequenceNumber,
            bytes32[] memory priceIds,
            uint256 publishTime
        ) = setupConsumerRequest(
                echo,
                defaultProvider,
                address(failingConsumer)
            );

        PythStructs.PriceFeed[] memory priceFeeds = createMockPriceFeeds(
            publishTime
        );
        mockParsePriceFeedUpdates(pyth, priceFeeds);
        bytes[] memory updateData = createMockUpdateData(priceFeeds);

        vm.expectEmit();
        emit PriceUpdateCallbackFailed(
            sequenceNumber,
            defaultProvider,
            priceIds,
            address(failingConsumer),
            "callback failed"
        );

        vm.prank(defaultProvider);
        echo.executeCallback(
            defaultProvider,
            sequenceNumber,
            updateData,
            priceIds
        );
    }
```
