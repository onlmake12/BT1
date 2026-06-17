### Title
Entropy `requestHelper` Silently Absorbs All Excess `msg.value` Into Protocol Fees Without Refund — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `requestHelper()` function in `Entropy.sol` enforces only a minimum fee check (`msg.value >= requiredFee`) but then credits the **entire** `msg.value − providerFee` to `accruedPythFeesInWei`. Any ETH sent above the required fee is permanently absorbed into the Pyth protocol fee pool with no refund to the caller. Integrating contracts that forward `msg.value` directly — a pattern explicitly shown in Pyth's own documentation — silently lose all excess ETH on every request.

---

### Finding Description

In `requestHelper()`, the fee accounting is:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The split is:
- `providerFee` → credited to the provider
- `msg.value − providerFee` → credited to Pyth's fee pool (`accruedPythFeesInWei`)

The correct split should be:
- `providerFee` → provider
- `pythFeeInWei` → Pyth fee pool
- `msg.value − requiredFee` → **refunded to caller**

Because `requiredFee = providerFee + pythFeeInWei`, the current code routes any overpayment (`msg.value − requiredFee`) into `accruedPythFeesInWei` rather than returning it. This is confirmed by the interface documentation:

> "Further note that excess value is *not* refunded to the caller." [2](#0-1) 

The same no-refund behavior is present in the legacy `request()` and `requestWithCallback()` paths: [3](#0-2) [4](#0-3) 

---

### Impact Explanation

Any ETH sent above `requiredFee` is permanently transferred to the Pyth protocol fee pool and is unrecoverable by the original sender. Two concrete loss paths exist:

1. **Wrapper contracts forwarding `msg.value` directly.** The Pyth documentation and SDK examples show the pattern `entropy.requestV2{value: msg.value}(...)` or `entropy.requestV2{value: requestFee}()` where `requestFee` is fetched from `getFeeV2()`. If a user sends more ETH than the exact fee to the wrapper contract (e.g., to ensure the transaction does not revert due to fee changes), the wrapper forwards the full `msg.value` and the excess is absorbed.

2. **Fee decrease between `getFeeV2()` query and transaction execution.** A provider can call `setProviderFee()` to lower their fee at any time. If a user queries `getFeeV2()` = X, constructs a transaction with `msg.value = X`, and the provider lowers their fee to Y before the transaction is mined, the user pays X but only Y + pythFee is required. The difference `X − (Y + pythFee)` is silently absorbed into `accruedPythFeesInWei`.

The absorbed ETH accrues to the Pyth admin-controlled fee pool and can be withdrawn by governance, constituting a direct, permanent loss of user funds.

---

### Likelihood Explanation

**High.** The pattern of forwarding `msg.value` directly to `requestV2` is the natural and documented integration pattern. The Pyth developer documentation explicitly shows:

```solidity
uint128 requestFee = entropy.getFeeV2();
if (msg.value < requestFee) revert("not enough fees");
uint64 sequenceNumber = entropy.requestV2{ value: requestFee }();
``` [5](#0-4) 

Any integrating contract that passes `msg.value` directly (rather than exactly `getFeeV2()`) silently loses the excess. Provider fee changes are permissionless for the provider and can occur at any block, making the race condition realistic on any chain with non-trivial block times.

---

### Recommendation

Refund excess `msg.value` to the caller after deducting exactly `requiredFee`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (requiredFee - providerFee); // only pythFeeInWei

// Refund excess
uint256 excess = msg.value - requiredFee;
if (excess > 0) {
    (bool ok, ) = msg.sender.call{value: excess}("");
    require(ok, "refund failed");
}
```

This matches the behavior of the Pyth price feed contract (`Pyth.sol`), which also only checks `msg.value >= requiredFee` but does not absorb the excess into a fee pool in the same unbounded way. [6](#0-5) 

---

### Proof of Concept

1. Provider registers with `feeInWei = 1000 wei`, Pyth fee = 100 wei → `getFeeV2() = 1100 wei`.
2. Integrating contract `Wrapper` implements:
   ```solidity
   function flip() external payable {
       entropy.requestV2{value: msg.value}();
   }
   ```
3. User calls `Wrapper.flip{value: 2000 wei}()`.
4. `requestHelper` executes:
   - `requiredFee = 1100`, check passes (`2000 >= 1100`)
   - `providerFee = 1000` → credited to provider
   - `accruedPythFeesInWei += 2000 − 1000 = 1000` (should be 100)
5. User loses 900 wei of excess to the Pyth fee pool. The provider receives their correct 1000 wei, but Pyth receives 1000 wei instead of 100 wei — the 900 wei overpayment is permanently absorbed. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L320-322)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function request(
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L344-346)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function requestWithCallback(
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** apps/developer-hub/content/docs/entropy/create-your-first-entropy-app.mdx (L91-101)
```text
// get the required fee
uint128 requestFee = entropy.getFeeV2();
// check if the user has sent enough fees
if (msg.value < requestFee) revert("not enough fees");

    // pay the fees and request a random number from entropy
    uint64 sequenceNumber = entropy.requestV2{ value: requestFee }();

    // emit event
    emit FlipRequested(sequenceNumber);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L77-79)
```text
        uint requiredFee = getTotalFee(totalNumUpdates);
        if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
    }
```
