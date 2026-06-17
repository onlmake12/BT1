### Title
Excess ETH Sent to Entropy Request Functions Is Permanently Captured Without Refund — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `requestHelper` internal function in `Entropy.sol`, called by all public request entry points (`requestWithCallback`, `requestV2`), consumes the **entire** `msg.value` by splitting it between the provider's accrued fees and the Pyth protocol treasury. Any ETH sent above the required fee is permanently captured by the protocol — it is never refunded to the caller. This is the direct EVM analog of the Dexter "amount sent" protection bug: a user who overpays (due to a fee race condition, a stale fee estimate, or a simple mistake) permanently loses the excess native token.

---

### Finding Description

In `requestHelper` (`Entropy.sol`):

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The entire `msg.value` is consumed. The provider receives `providerFee`, and **all remaining ETH** (`msg.value - providerFee`) is credited to `_state.accruedPythFeesInWei` — the Pyth protocol treasury. There is no branch that refunds `msg.value - requiredFee` to `msg.sender`.

This behavior is propagated through every public payable entry point:

- `requestWithCallback` → calls `requestHelper`
- `requestV2(address, bytes32, uint32)` → calls `requestHelper` [2](#0-1) [3](#0-2) 

The interface documentation acknowledges this explicitly:

> "Note that excess value is *not* refunded to the caller." [4](#0-3) [5](#0-4) 

The documentation acknowledges the behavior but does not mitigate it. The root cause — the absence of a refund — is entirely within Pyth's own contract code.

---

### Impact Explanation

Any user who sends `msg.value > requiredFee` permanently loses the excess ETH to the Pyth treasury. Concretely:

- A user calls `getFeeV2()` off-chain and obtains fee `F`.
- Between that read and the transaction landing on-chain, the provider calls `setProviderFee` and raises the fee to `F'`.
- The user's transaction reverts with `InsufficientFee` — **or** the user defensively adds a buffer (e.g., `msg.value = F + buffer`) to avoid a revert.
- In the buffer case, `buffer` wei is permanently captured by the Pyth treasury.

This is a direct loss of user funds with no recovery path. The ETH is not locked — it is credited to `accruedPythFeesInWei` and becomes withdrawable by Pyth governance — but the original sender has no recourse.

---

### Likelihood Explanation

- **Fee volatility:** Provider fees can change at any time via `setProviderFee` / `setProviderFeeAsFeeManager`. [6](#0-5) 
- **Defensive overpayment:** Integrators and wallets routinely add a small buffer to avoid `InsufficientFee` reverts. Every such call silently donates the buffer to the Pyth treasury.
- **No warning at call site:** The revert path only triggers for underpayment; overpayment silently succeeds, giving the user no indication that funds were lost.
- **Unprivileged entry:** Any EOA or contract can call `requestWithCallback` / `requestV2` — no special role required.

---

### Recommendation

Add a refund of excess ETH at the end of `requestHelper`:

```solidity
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool ok, ) = msg.sender.call{value: excess}("");
    require(ok, "refund failed");
}
```

Alternatively, revert if `msg.value > requiredFee` (strict-exact payment), consistent with the short-term recommendation in the Dexter report.

---

### Proof of Concept

1. Deploy Entropy on a local fork.
2. Register a provider with `feeInWei = 100`.
3. Call `requestV2{value: 200}(provider, userContribution, 0)` — sending 2× the required fee.
4. Observe that `getAccruedPythFees()` increases by `200 - providerFee` (i.e., the excess is captured).
5. Observe that `msg.sender`'s balance decreases by the full 200 wei, not just the required fee.
6. Confirm there is no refund event and no way for the caller to recover the excess. [1](#0-0)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L346-356)
```text
    function requestWithCallback(
        address provider,
        bytes32 userContribution
    ) public payable override returns (uint64) {
        return
            requestV2(
                provider,
                userContribution,
                0 // Passing 0 will assign the request the provider's default gas limit
            );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L358-390)
```text
    function requestV2(
        address provider,
        bytes32 userContribution,
        uint32 gasLimit
    ) public payable override returns (uint64) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );

        emit RequestedWithCallback(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            EntropyStructConverter.toV1Request(req)
        );
        emit EntropyEventsV2.Requested(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            uint32(req.gasLimit10k) * TEN_THOUSAND,
            bytes("")
        );
        return req.sequenceNumber;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-827)
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
        emit EntropyEventsV2.ProviderFeeUpdated(
            msg.sender,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L66-68)
```text
    // This method will revert unless the caller provides a sufficient fee (at least `getFee(provider)`) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function requestWithCallback(
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
