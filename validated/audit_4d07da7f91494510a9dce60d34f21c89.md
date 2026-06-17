### Title
Provider Fee Frontrunning Drains User Overpayment With No Refund — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The Entropy contract's `requestHelper()` function consumes the entire `msg.value` without refunding any excess. Because a registered provider can atomically raise their fee via `setProviderFee()` at any time, a malicious provider can frontrun a user's `requestV2()` transaction — raising their fee to just below the user's `msg.value` — and extract the user's entire buffer payment. The user has no mechanism to specify a maximum acceptable fee.

---

### Finding Description

In `requestHelper()`, fee accounting is:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The entire `msg.value` is consumed: `providerFee` goes to the provider and `msg.value - providerFee` goes to Pyth's accrued fees. **No excess is ever returned to the caller.** This is explicitly documented in the interface:

> "Further note that excess value is *not* refunded to the caller." [2](#0-1) 

A provider can change their fee at any time with no delay or cap:

```solidity
function setProviderFee(uint128 newFeeInWei) external override {
    ...
    provider.feeInWei = newFeeInWei;
``` [3](#0-2) 

Because fees can change between a user's `getFeeV2()` query and their `requestV2()` execution, users are advised (and in practice forced) to send a buffer above the quoted fee to avoid reverts. A malicious provider can exploit this by frontrunning the user's transaction with a `setProviderFee()` call that raises the fee to just below the user's `msg.value`, capturing the entire buffer as provider revenue.

The same pattern exists in `Echo.sol`'s `requestPriceUpdatesWithCallback()`, where `req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei)` stores the full overpayment as provider-credited fees with no refund path. [4](#0-3) 

---

### Impact Explanation

A malicious registered provider can extract the full ETH buffer a user sends above the quoted fee. Since the IEntropyV2 interface explicitly warns that excess is not refunded, users who want reliable request submission must send a buffer — and that buffer is the attack surface. The provider profits by the difference between the original fee and the raised fee (up to `msg.value - 1`). Users have no on-chain mechanism to bound the fee they are willing to accept.

**Impact: Medium** — direct ETH loss to users proportional to their buffer amount; no protocol invariant is broken, but user funds are extracted by a malicious provider.

---

### Likelihood Explanation

**Likelihood: Low** — requires a registered provider who is willing to act maliciously (risking reputation and future business), who also monitors the public mempool to identify and frontrun specific user transactions. On chains with private mempools or fast block times the attack window is narrow. However, the no-refund design makes any fee increase between query and execution a guaranteed loss for users who send buffers.

---

### Recommendation

1. Add a `maxFee` parameter to `requestV2()` variants so users can specify the maximum fee they are willing to pay; revert if `requiredFee > maxFee`.
2. Alternatively, refund `msg.value - requiredFee` to `msg.sender` at the end of `requestHelper()` so users can safely send a buffer without financial risk.
3. Consider a time-lock or minimum notice period on `setProviderFee()` increases to prevent same-block frontrunning.

---

### Proof of Concept

1. Provider is registered with `feeInWei = 1000 wei`. Pyth fee is `100 wei`. `getFeeV2(provider, 0)` returns `1100 wei`.
2. Alice calls `requestV2(provider, userRandom, 0)` with `msg.value = 2200 wei` (2× buffer to avoid revert risk).
3. Provider observes Alice's pending transaction in the mempool and frontruns with `setProviderFee(2099)`.
4. Alice's transaction executes: `requiredFee = getFeeV2(provider, 0) = 2099 + 100 = 2199 wei`. Since `msg.value (2200) >= 2199`, no revert.
5. `providerInfo.accruedFeesInWei += 2099` (provider captures 2099 instead of 1000).
6. `accruedPythFeesInWei += 2200 - 2099 = 101`.
7. Alice paid 2200 wei for a service she expected to cost 1100 wei. The extra 1000 wei is permanently lost — 999 wei to the provider, 1 wei to Pyth. Alice has no recourse. [5](#0-4) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-84)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```
