### Title
Wrong Address (`msg.sender`) Emitted as Provider in `Echo.emitPriceUpdate` Instead of `providerToCredit` - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.executeCallback`, the fee is correctly credited to the caller-supplied `providerToCredit` address, but the internal `emitPriceUpdate` helper emits the `PriceUpdateExecuted` event using `msg.sender` as the provider argument instead of `providerToCredit`. When a third party executes the callback after the exclusivity period (a fully supported and intended flow), `msg.sender != providerToCredit`, causing the event to permanently misattribute the execution to the wrong address.

---

### Finding Description

`Echo.executeCallback` accepts a `providerToCredit` parameter that identifies which provider should receive the fee credit for fulfilling the request. After the exclusivity period expires, any address may call `executeCallback` and specify any registered provider as `providerToCredit`.

The fee accounting is correct:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [1](#0-0) 

However, the `emitPriceUpdate` helper, called on successful callback execution, emits the `PriceUpdateExecuted` event with `msg.sender` as the provider:

```solidity
emit PriceUpdateExecuted(
    sequenceNumber,
    msg.sender,   // ← should be providerToCredit
    priceIds,
    ...
);
``` [2](#0-1) 

The `providerToCredit` address is available in `executeCallback` but is never forwarded into `emitPriceUpdate`:

```solidity
function emitPriceUpdate(
    uint64 sequenceNumber,
    bytes32[] memory priceIds,
    PythStructs.PriceFeed[] memory priceFeeds
) internal {
``` [3](#0-2) 

The parallel to the YieldFi bug is exact: a downstream call (here, event emission) receives `msg.sender` where the explicitly-passed address (`providerToCredit`) should be used.

---

### Impact Explanation

Any off-chain system — including the Fortuna keeper, analytics dashboards, or provider reputation tracking — that consumes `PriceUpdateExecuted` events to determine which provider fulfilled a request will receive incorrect data. The event will attribute the execution to `msg.sender` (the transaction submitter), while the actual fee credit and the intended provider identity is `providerToCredit`. In the post-exclusivity scenario where a third-party relayer calls `executeCallback` on behalf of a provider, every emitted event will carry the wrong provider address. This breaks the auditability of the Echo protocol's provider attribution and can corrupt off-chain fee-tracking or SLA-monitoring systems.

---

### Likelihood Explanation

The `executeCallback` function is callable by any unprivileged address after the exclusivity period (`_state.exclusivityPeriodSeconds`, default 15 seconds) elapses:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(providerToCredit == req.provider, "Only assigned provider during exclusivity period");
}
``` [4](#0-3) 

After this window, any caller can supply a `providerToCredit` that differs from `msg.sender`. The Fortuna keeper infrastructure is designed to call `executeCallback` as a third-party relayer, making `msg.sender != providerToCredit` the normal post-exclusivity execution path, not an edge case.

---

### Recommendation

Pass `providerToCredit` into `emitPriceUpdate` and use it in the event emission instead of `msg.sender`:

```solidity
function emitPriceUpdate(
    uint64 sequenceNumber,
    address providerToCredit,   // add parameter
    bytes32[] memory priceIds,
    PythStructs.PriceFeed[] memory priceFeeds
) internal {
    ...
    emit PriceUpdateExecuted(
        sequenceNumber,
        providerToCredit,   // use providerToCredit, not msg.sender
        priceIds,
        ...
    );
}
```

Update the call site in `executeCallback` accordingly:

```solidity
emitPriceUpdate(sequenceNumber, providerToCredit, priceIds, priceFeeds);
```

---

### Proof of Concept

1. Register two providers: `providerA` (original) and `providerB` (third party).
2. Consumer calls `requestPriceUpdatesWithCallback` specifying `providerA`.
3. Wait for `exclusivityPeriodSeconds` to elapse.
4. `providerB` calls `executeCallback(providerA, sequenceNumber, updateData, priceIds)` — this is valid post-exclusivity.
5. Observe: `_state.providers[providerA].accruedFeesInWei` is incremented (correct), but `PriceUpdateExecuted` is emitted with `providerB` (i.e., `msg.sender`) as the provider field instead of `providerA` (i.e., `providerToCredit`).

```solidity
function testWrongProviderInEvent() public {
    // providerB executes on behalf of providerA after exclusivity
    vm.warp(block.timestamp + echo.getExclusivityPeriod() + 1);
    vm.prank(providerB);
    // providerToCredit = providerA, msg.sender = providerB
    echo.executeCallback(providerA, sequenceNumber, updateData, priceIds);
    // PriceUpdateExecuted event will incorrectly show providerB, not providerA
}
``` [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L114-121)
```text
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L204-208)
```text
    function emitPriceUpdate(
        uint64 sequenceNumber,
        bytes32[] memory priceIds,
        PythStructs.PriceFeed[] memory priceFeeds
    ) internal {
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L224-232)
```text
        emit PriceUpdateExecuted(
            sequenceNumber,
            msg.sender,
            priceIds,
            prices,
            conf,
            expos,
            publishTimes
        );
```
