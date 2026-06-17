### Title
No Maximum Fee Guard in Entropy Request Functions Causes Permanent Loss of Excess ETH — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `request`, `requestWithCallback`, and `requestV2` family of functions in `Entropy.sol` accept any `msg.value ≥ requiredFee` but **never refund the excess**. All surplus ETH beyond the provider's fee is silently credited to Pyth's treasury (`accruedPythFeesInWei`). Because providers can update their fee at any time with no timelock via `setProviderFee`, and because no `maxFee` guard exists in any request entry-point, users who defensively over-pay (a standard protective pattern) or whose transactions are front-run by a provider fee increase permanently lose ETH.

---

### Finding Description

In `requestHelper`, fee accounting is:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The intended split is `providerFee` → provider, `pythFeeInWei` → Pyth. But the actual code credits `msg.value − providerFee` to Pyth. Any ETH above `providerFee` — including the entire `pythFeeInWei` portion **plus any user-supplied buffer** — is absorbed by Pyth with no refund path. [2](#0-1) 

Providers can raise their fee instantly and without restriction:

```solidity
function setProviderFee(uint128 newFeeInWei) external override {
    ...
    provider.feeInWei = newFeeInWei;
``` [3](#0-2) 

None of the public entry-points (`request`, `requestWithCallback`, `requestV2`) accept a `maxFee` parameter: [4](#0-3) [5](#0-4) 

The NatSpec itself acknowledges the risk but provides no on-chain protection: *"Note that provider fees can change over time … excess value is not refunded to the caller."* [6](#0-5) 

---

### Impact Explanation

**Scenario A — defensive over-payment (no malice required):**
A user queries `getFee(provider)` = F, then submits `msg.value = F + buffer` to guard against a fee increase between query and inclusion. The transaction succeeds, but `buffer` is permanently credited to `accruedPythFeesInWei` and is unrecoverable by the user.

**Scenario B — provider front-run:**
1. User queries `getFee(provider)` = F, submits `msg.value = F + buffer`.
2. Provider calls `setProviderFee(F + buffer/2)` in the same block, ahead of the user.
3. User's tx succeeds (new fee ≤ msg.value), but `buffer/2` is silently lost to Pyth.

In both cases the user receives exactly the same service (one sequence number) regardless of how much they overpay. The excess is a one-way transfer to Pyth's treasury with no recourse.

---

### Likelihood Explanation

- Defensive over-payment is a standard, widely-recommended pattern in EVM development to avoid reverts from fee changes; many integrators and SDK wrappers will naturally add a small buffer.
- Providers are permissionless and can update fees at any time with a single transaction — no governance delay, no timelock.
- The SDK documentation and NatSpec explicitly warn that fees can change, which encourages users to over-pay, making the loss path more likely, not less.

---

### Recommendation

1. **Add a `maxFee` parameter** to `request`, `requestWithCallback`, and `requestV2`. Revert if `requiredFee > maxFee`.
2. **Alternatively, refund excess `msg.value`** after the fee split:
   ```solidity
   if (msg.value > requiredFee) {
       (bool ok,) = msg.sender.call{value: msg.value - requiredFee}("");
       require(ok, "refund failed");
   }
   ```
   This mirrors the standard slippage-protection pattern used in AMMs and token sale contracts.

---

### Proof of Concept

```solidity
function testOverpaymentLost() public {
    address user = makeAddr("user");
    uint128 fee = random.getFee(provider1); // e.g. 200 wei

    // User sends 2x the fee as a defensive buffer
    vm.deal(user, fee * 2);
    vm.prank(user);
    random.requestWithCallback{value: fee * 2}(provider1, bytes32(uint256(42)));

    // The extra `fee` wei is now in accruedPythFees, not refunded
    assertEq(random.getAccruedPythFees(), _state.pythFeeInWei + fee);
    // user lost `fee` wei with no recourse
}
```

The root cause is in `requestHelper` at lines 238–239 of `Entropy.sol`: `_state.accruedPythFeesInWei` absorbs all of `msg.value − providerFee`, not just `pythFeeInWei`. [7](#0-6)

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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L46-52)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function request(
        address provider,
        bytes32 userCommitment,
        bool useBlockHash
    ) external payable returns (uint64 assignedSequenceNumber);
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L94-101)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
    function requestV2(
        address provider,
        bytes32 userRandomNumber,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);
```
