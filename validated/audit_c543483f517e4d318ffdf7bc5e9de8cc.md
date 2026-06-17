### Title
Users Cannot Specify Maximum Fee (Slippage Protection) When Requesting Entropy — (`Entropy.sol`)

---

### Summary

In `Entropy.sol`, all `requestV2` / `requestWithCallback` variants read the provider fee from live contract state at execution time but expose no `maxFee` parameter. Because excess `msg.value` is **never refunded** and the provider can change their fee at any time, a user who sends a buffer above the quoted fee can silently overpay, with the surplus permanently credited to Pyth protocol fees rather than returned to the caller.

---

### Finding Description

Every entry-point for requesting randomness (`requestV2()`, `requestV2(uint32)`, `requestV2(address,uint32)`, `requestV2(address,bytes32,uint32)`, `requestWithCallback`) ultimately calls `requestHelper`, which performs the following fee accounting:

```solidity
// Entropy.sol lines 234-239
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The fee is composed of `provider.feeInWei + _state.pythFeeInWei`. The provider can update `feeInWei` at any time with no time-lock:

```solidity
// Entropy.sol lines 810-820
function setProviderFee(uint128 newFeeInWei) external override {
    ...
    provider.feeInWei = newFeeInWei;
    ...
}
``` [2](#0-1) 

The `IEntropyV2` interface documents this risk but provides no mitigation:

> *"Note that the fee can change over time … Further note that excess value is **not** refunded to the caller."* [3](#0-2) 

None of the four `requestV2` overloads accept a `maxFee` parameter: [4](#0-3) 

The accounting line `_state.accruedPythFeesInWei += (msg.value - providerFee)` means any `msg.value` above `providerFee` is credited to the Pyth treasury, not returned to the user. [5](#0-4) 

---

### Impact Explanation

Two concrete loss paths exist for an unprivileged user:

**Path A — Silent overpayment (financial loss):**
1. User calls `getFeeV2()` off-chain and observes fee = X.
2. User submits `requestV2{value: X + Δ}()` with a small buffer Δ to avoid a revert from a minor fee change.
3. Provider calls `setProviderFee` to raise `feeInWei` so that `requiredFee` = X + Δ/2 (still ≤ user's `msg.value`).
4. Transaction succeeds; user pays X + Δ/2 instead of X. The surplus Δ/2 is permanently absorbed into `_state.accruedPythFeesInWei` — no refund path exists.

**Path B — Forced revert (DoS / griefing):**
1. User submits `requestV2{value: X}()` with the exact quoted fee.
2. Provider raises fee to X+1 before the transaction is mined.
3. Transaction reverts with `InsufficientFee`. User wastes gas and must retry.

Both paths are reachable by any unprivileged user interacting with the Entropy contract. Path A results in a direct, unrecoverable ETH loss proportional to the buffer the user chose to send.

---

### Likelihood Explanation

- The provider (`setProviderFee`) and Pyth governance (`setPythFee`) can both change fee components at any time with no time-lock or delay.
- Integrating contracts (e.g., a DeFi protocol wrapping Entropy) routinely send a buffer above the quoted fee to avoid reverts, making Path A the common case.
- The NatSpec itself warns that "the fee can change over time," confirming this is an expected operational scenario, not a theoretical edge case.
- No special privilege beyond being a registered provider is required to trigger Path A; the default provider (Fortuna) is the most likely actor, but any registered provider qualifies.

---

### Recommendation

Add a `maxFee` parameter to `requestV2` (and `requestWithCallback`) so callers can specify an upper bound on the fee they are willing to pay:

```solidity
function requestV2(
    address provider,
    bytes32 userRandomNumber,
    uint32 gasLimit,
    uint128 maxFee          // NEW: revert if requiredFee > maxFee
) external payable returns (uint64 assignedSequenceNumber);
```

Inside `requestHelper`, add:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (maxFee != 0 && requiredFee > maxFee) revert EntropyErrors.FeeTooHigh();
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
```

Additionally, refund excess `msg.value` to the caller:

```solidity
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool ok,) = msg.sender.call{value: excess}("");
    require(ok, "refund failed");
}
```

This mirrors the slippage-protection pattern recommended in the referenced Astaria report and eliminates both the overpayment and the forced-revert griefing vectors.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import "./IEntropyV2.sol";

contract SlippagePoC {
    IEntropyV2 public entropy;
    address public provider;

    constructor(address _entropy, address _provider) {
        entropy = IEntropyV2(_entropy);
        provider = _provider;
    }

    // Step 1: user calls this, sending msg.value = quotedFee + buffer
    function requestWithBuffer(uint128 buffer) external payable {
        uint128 quotedFee = entropy.getFeeV2(provider, 0);
        // User sends quotedFee + buffer to avoid revert
        require(msg.value == quotedFee + buffer, "send exact quoted+buffer");

        // Step 2 (off-chain, same block): provider calls setProviderFee(quotedFee + buffer/2)
        // This raises the fee so requiredFee = quotedFee + buffer/2 <= msg.value
        // Transaction succeeds, but user pays buffer/2 extra — permanently lost to Pyth fees.

        entropy.requestV2{value: msg.value}(provider, 0);
        // No refund of excess. User has lost `buffer/2` wei with no recourse.
    }
}
```

The `_state.accruedPythFeesInWei += (msg.value - providerFee)` line in `requestHelper` confirms the surplus is irrecoverably credited to the protocol treasury. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L26-101)
```text
    function requestV2()
        external
        payable
        returns (uint64 assignedSequenceNumber);

    /// @notice Request a random number using the default provider with specified gas limit
    /// @param gasLimit The gas limit for the callback function.
    /// @return assignedSequenceNumber A unique identifier for this request
    /// @dev The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    /// The `entropyCallback` method on that interface will receive a callback with the returned sequence number and
    /// the generated random number.
    ///
    /// `entropyCallback` will be run with the `gasLimit` provided to this function.
    /// The `gasLimit` will be rounded up to a multiple of 10k (e.g., 19000 -> 20000), and furthermore is lower bounded
    /// by the provider's configured default limit.
    ///
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(gasLimit)`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2(gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
    ///
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
    function requestV2(
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);

    /// @notice Request a random number from a specific provider with specified gas limit
    /// @param provider The address of the provider to request from
    /// @param gasLimit The gas limit for the callback function
    /// @return assignedSequenceNumber A unique identifier for this request
    /// @dev The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    /// The `entropyCallback` method on that interface will receive a callback with the returned sequence number and
    /// the generated random number.
    ///
    /// `entropyCallback` will be run with the `gasLimit` provided to this function.
    /// The `gasLimit` will be rounded up to a multiple of 10k (e.g., 19000 -> 20000), and furthermore is lower bounded
    /// by the provider's configured default limit.
    ///
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
    ///
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);

    /// @notice Request a random number from a specific provider with a user-provided random number and gas limit
    /// @param provider The address of the provider to request from
    /// @param userRandomNumber A random number provided by the user for additional entropy
    /// @param gasLimit The gas limit for the callback function. Pass 0 to get a sane default value -- see note below.
    /// @return assignedSequenceNumber A unique identifier for this request
    /// @dev The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    /// The `entropyCallback` method on that interface will receive a callback with the returned sequence number and
    /// the generated random number.
    ///
    /// `entropyCallback` will be run with the `gasLimit` provided to this function.
    /// The `gasLimit` will be rounded up to a multiple of 10k (e.g., 19000 -> 20000), and furthermore is lower bounded
    /// by the provider's configured default limit.
    ///
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
    function requestV2(
        address provider,
        bytes32 userRandomNumber,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);
```
