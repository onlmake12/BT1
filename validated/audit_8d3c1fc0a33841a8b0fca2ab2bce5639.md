### Title
Missing Maximum Fee Cap in `setProviderFee()` Enables Provider Fee Front-Running — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `setProviderFee()` function in `Entropy.sol` allows any registered Entropy provider to set their `feeInWei` to an arbitrarily large `uint128` value with no upper bound check. Combined with the documented behavior that **excess ETH sent to `request()`/`requestWithCallback()`/`requestV2()` is not refunded**, a malicious provider can front-run user transactions to extract the user's ETH buffer or cause a persistent DoS on their own provider endpoint.

---

### Finding Description

`setProviderFee()` performs only a registration check before writing the caller-supplied value directly to storage:

```solidity
// Entropy.sol lines 810–827
function setProviderFee(uint128 newFeeInWei) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) {
        revert EntropyErrors.NoSuchProvider();
    }
    uint128 oldFeeInWei = provider.feeInWei;
    provider.feeInWei = newFeeInWei;   // ← no upper-bound check
    ...
}
``` [1](#0-0) 

There is no maximum cap on `newFeeInWei`. Any address that has previously called `register()` (an unprivileged, permissionless action) can call `setProviderFee(type(uint128).max)` at any time.

The `requestHelper` that processes all user requests explicitly states and implements the no-refund policy:

```solidity
// Entropy.sol lines 234–239
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [2](#0-1) 

The IEntropyV2 interface also documents this explicitly:

> "Note that excess value is *not* refunded to the caller." [3](#0-2) 

The same uncapped setter exists for the fee manager role via `setProviderFeeAsFeeManager()`: [4](#0-3) 

---

### Impact Explanation

**Attack vector 1 — Fee extraction via front-running:**

1. A provider registers with a low, reasonable `feeInWei` (e.g., 0.001 ETH).
2. A user calls `getFee(provider)` off-chain and receives the low fee quote.
3. The user, following best practice, sends `msg.value = 2 × quoted_fee` as a buffer to avoid reverts from minor fee changes.
4. The provider observes the pending transaction in the mempool and front-runs it with `setProviderFee(2 × quoted_fee - pythFee)`.
5. The user's transaction executes: `requiredFee` now equals `msg.value`, the check passes, and the provider accrues the full `msg.value - pythFee` — double the original fee.
6. The user receives their random number but paid 2× the expected cost with no recourse.

**Attack vector 2 — Persistent DoS:**

A provider sets `feeInWei` to `type(uint128).max`. Any call to `getFeeV2(provider, ...)` will overflow and revert, making the provider permanently unusable until the fee is lowered. If this provider is the default provider (set by admin), all calls to the no-argument `requestV2()` variant are bricked. [5](#0-4) 

---

### Likelihood Explanation

- **Permissionless entry**: Any address can call `register()` to become a provider; no admin approval is required.
- **No time-lock or delay**: `setProviderFee()` takes effect immediately in the same block, making front-running straightforward.
- **No user-side protection**: Users have no way to specify a `maxFeeInWei` parameter (analogous to `amountOutMin` in DEX swaps) to bound what they are willing to pay.
- **No-refund policy amplifies harm**: The explicit no-refund design means any ETH sent above the minimum is permanently captured by the contract and credited to the provider or Pyth.

---

### Recommendation

1. **Add a maximum fee cap** in `setProviderFee()` and `register()`:
   ```solidity
   uint128 public constant MAX_PROVIDER_FEE = 1 ether; // or governance-controlled
   require(newFeeInWei <= MAX_PROVIDER_FEE, "Fee exceeds maximum");
   ```

2. **Add a user-supplied `maxFeeInWei` parameter** to `request()`, `requestWithCallback()`, and `requestV2()` so callers can bound what they are willing to pay, analogous to `amountOutMin` in DEX swaps:
   ```solidity
   if (requiredFee > maxFeeInWei) revert EntropyErrors.FeeTooHigh();
   ```

3. **Refund excess ETH** to the caller after deducting `requiredFee`, eliminating the economic incentive for fee front-running.

---

### Proof of Concept

```solidity
// 1. Attacker registers as a provider with a low fee
entropy.register{value: ...}(0.001 ether, commitment, metadata, chainLength, uri);

// 2. User queries fee off-chain: getFee(attacker) → 0.001 ether + pythFee
// User prepares tx: requestWithCallback{value: 0.002 ether}(attacker, userRandom)

// 3. Attacker front-runs (higher gas):
entropy.setProviderFee(0.002 ether - pythFee);  // no cap check, succeeds immediately

// 4. User's tx executes:
//    requiredFee = 0.002 ether  (matches msg.value exactly)
//    providerInfo.accruedFeesInWei += 0.002 ether - pythFee  ← attacker earns 2×
//    _state.accruedPythFeesInWei += pythFee
//    User paid 2× the expected fee with no refund.
```

Root cause: `setProviderFee()` at line 819 writes `newFeeInWei` to storage with no upper-bound validation. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-239)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-293)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-820)
```text
    function setProviderFee(uint128 newFeeInWei) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
        uint128 oldFeeInWei = provider.feeInWei;
        provider.feeInWei = newFeeInWei;
        emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L829-855)
```text
    function setProviderFeeAsFeeManager(
        address provider,
        uint128 newFeeInWei
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }

        uint128 oldFeeInWei = providerInfo.feeInWei;
        providerInfo.feeInWei = newFeeInWei;

        emit ProviderFeeUpdated(provider, oldFeeInWei, newFeeInWei);
        emit EntropyEventsV2.ProviderFeeUpdated(
            provider,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L43-44)
```text
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2(gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
