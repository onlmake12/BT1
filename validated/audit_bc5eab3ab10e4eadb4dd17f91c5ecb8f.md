### Title
Excess ETH Permanently Absorbed Into Protocol Fees in Entropy Request Functions — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The public payable entry points `request`, `requestV2`, and `requestWithCallback` in `Entropy.sol` all funnel through `requestHelper`, which checks that `msg.value >= requiredFee` but never refunds any surplus. Every wei above the required fee is silently credited to `_state.accruedPythFeesInWei` and is permanently lost to the caller.

---

### Finding Description

`requestHelper` computes the required fee and then distributes **all** of `msg.value` between the provider and Pyth:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The surplus `(msg.value - requiredFee)` is added to `accruedPythFeesInWei` rather than returned to `msg.sender`. The three public payable callers each carry an inline comment acknowledging this:

> *"Note that excess value is **not** refunded to the caller."* [2](#0-1) [3](#0-2) 

The same pattern exists in `Echo.sol` `requestPriceUpdatesWithCallback`, where excess ETH is stored as `req.fee` and later paid out to the fulfilling provider:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [4](#0-3) 

And in `Pyth.sol` `updatePriceFeeds`, which is payable but performs no refund after the fee check:

```solidity
uint requiredFee = getTotalFee(totalNumUpdates);
if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
``` [5](#0-4) 

By contrast, `PythLazer.sol` `verifyUpdate` correctly refunds excess:

```solidity
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [6](#0-5) 

---

### Impact Explanation

Any caller of `request` / `requestV2` / `requestWithCallback` who sends even 1 wei above `getFeeV2(provider, gasLimit)` permanently loses that surplus to Pyth's fee pool. In `Echo`, the surplus is redirected to the fulfilling provider's balance. In `Pyth.sol` `updatePriceFeeds`, it accumulates in the contract and is only recoverable via a privileged governance `WithdrawFee` action. In all three cases the original sender has no recourse.

---

### Likelihood Explanation

Fee estimation in EVM tooling (ethers.js, web3.js, wallets) commonly adds a small buffer to `msg.value` to avoid `InsufficientFee` reverts when fees fluctuate. Smart-contract integrators that call `getFeeV2` in one block and submit the transaction in a later block may also overpay if the fee was raised in between. Both scenarios are realistic for any unprivileged Entropy user or Echo requester.

---

### Recommendation

Add a refund at the end of `requestHelper` (Entropy) and `requestPriceUpdatesWithCallback` (Echo):

```solidity
// Entropy – inside requestHelper, after fee accounting
uint128 surplus = SafeCast.toUint128(msg.value) - requiredFee;
if (surplus > 0) {
    (bool ok, ) = payable(msg.sender).call{value: surplus}("");
    require(ok, "refund failed");
}
```

For `Pyth.sol` `updatePriceFeeds`, apply the same pattern after the fee check. `PythLazer.sol` already demonstrates the correct pattern and can serve as a reference.

---

### Proof of Concept

1. Deploy Entropy on a testnet with a provider whose `getFeeV2` returns `1000 wei`.
2. Call `requestV2{value: 1100 wei}(provider, userContribution, gasLimit)`.
3. Observe that `_state.accruedPythFeesInWei` increases by `1100 - providerFee` (absorbing the 100 wei surplus) and the caller's balance decreases by the full 1100 wei.
4. Confirm there is no mechanism for the caller to recover the 100 wei surplus. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L320-326)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function request(
        address provider,
        bytes32 userCommitment,
        bool useBlockHash
    ) public payable override returns (uint64 assignedSequenceNumber) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L344-349)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function requestWithCallback(
        address provider,
        bytes32 userContribution
    ) public payable override returns (uint64) {
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-84)
```text
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L77-79)
```text
        uint requiredFee = getTotalFee(totalNumUpdates);
        if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L75-77)
```text
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
