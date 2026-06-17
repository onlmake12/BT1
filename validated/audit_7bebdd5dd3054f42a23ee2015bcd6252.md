### Title
Gas Manipulation Causes Permanent Fund Loss in `Echo.executeCallback()` with No Recovery Path - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.executeCallback()` credits the provider and permanently clears the request **before** executing the consumer callback, with no `gasleft()` validation. An unprivileged caller can supply a gas amount that is sufficient to pass all pre-callback checks but insufficient to actually run the callback. The callback silently fails via try-catch, the request is gone, and the consumer's funds are permanently lost with no recovery mechanism.

### Finding Description

In `Echo.executeCallback()`, the execution order is:

1. Validate request and price IDs
2. Parse price feeds via Pyth
3. **Credit the provider** (`_state.providers[providerToCredit].accruedFeesInWei += ...`)
4. **Permanently clear the request** (`clearRequest(sequenceNumber)`)
5. Attempt the callback with `req.callbackGasLimit` gas in a try-catch [1](#0-0) 

The function is `external payable` with no access control — anyone can call it: [2](#0-1) 

There is **no `gasleft()` check** anywhere in `Echo.sol` before the callback is invoked: [3](#0-2) 

Due to EVM's 63/64 gas forwarding rule (CALL opcode), if the outer transaction has only `callbackGasLimit + overhead` gas, the sub-call receives at most `(callbackGasLimit + overhead) * 63/64` gas — potentially less than `callbackGasLimit`. The callback then fails with an out-of-gas error, which is silently swallowed by the bare `catch` block. The request has already been cleared and the provider already credited, so there is no retry path.

Compare this to `Entropy.revealWithCallback()`, which explicitly validates gas sufficiency before proceeding: [4](#0-3) 

Entropy reverts with `InsufficientGas` when the calling context lacked sufficient gas, and maintains a `CALLBACK_FAILED` state for recovery. Echo has neither protection. [5](#0-4) 

The `EchoState.Request` struct stores `callbackGasLimit` as a `uint32`, which is the only gas-related field — there is no failure state or retry mechanism: [6](#0-5) 

The existing test `testExecuteCallbackWithInsufficientGas` acknowledges the problem but only tests the case where the outer call itself runs out of gas entirely (full OOG revert). It does not test the more dangerous case where the outer call succeeds but the callback silently fails: [7](#0-6) 

### Impact Explanation

A consumer who paid a fee for a price update callback permanently loses their funds. The request is cleared and cannot be retried. The provider collects the fee without having delivered the callback. This breaks the core service guarantee of Echo: that paying the fee ensures the callback will be executed. Unlike Entropy (which has `CALLBACK_FAILED` state and recovery), Echo has no recovery path once the request is cleared.

### Likelihood Explanation

`executeCallback()` is permissionless — any address can call it. An attacker can front-run the legitimate provider's fulfillment transaction and call `executeCallback()` with a gas amount carefully chosen to pass all pre-callback checks but leave insufficient gas for the callback. The attacker can also be the `providerToCredit` address, collecting the fee while ensuring the callback fails. The 63/64 EVM gas forwarding rule makes the exact gas threshold calculable off-chain.

### Recommendation

Add a `gasleft()` check before the callback, mirroring Entropy's pattern:

```solidity
require(
    (gasleft() * 31) / 32 >= req.callbackGasLimit,
    "Insufficient gas for callback"
);
```

Additionally, restructure the function to follow checks-effects-interactions properly: do not credit the provider or clear the request until after the callback succeeds. If silent callback failure is desired (for provider UX), introduce a `CALLBACK_FAILED` state analogous to Entropy's, allowing the consumer to retry or recover funds.

### Proof of Concept

```solidity
// Attacker calls executeCallback with gas = callbackGasLimit + ~50_000
// (enough to pass all checks, but 63/64 rule means callback gets < callbackGasLimit)
echo.executeCallback{gas: req.callbackGasLimit + 50_000}(
    attackerAddress,   // providerToCredit = attacker collects fee
    sequenceNumber,
    updateData,
    priceIds
);
// Result:
// - attackerAddress.accruedFeesInWei increased by req.fee
// - request cleared (sequenceNumber no longer active)
// - consumer._echoCallback() never executed (OOG, caught silently)
// - consumer has no recourse
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-111)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-201)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);

        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
        // After successful callback, update firstUnfulfilledSeq if needed
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }

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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L12-29)
```text
    struct Request {
        // Slot 1: 8 + 8 + 4 + 12 = 32 bytes
        uint64 sequenceNumber;
        uint64 publishTime;
        uint32 callbackGasLimit;
        uint96 fee;
        // Slot 2: 20 + 12 = 32 bytes
        address requester;
        // 12 bytes padding

        // Slot 3: 20 + 12 = 32 bytes
        address provider;
        // 12 bytes padding

        // Dynamic array starts at its own slot
        // Store only first 8 bytes of each price ID to save gas
        bytes8[] priceIdPrefixes;
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
