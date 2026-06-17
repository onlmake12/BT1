### Title
Entropy `requestHelper` Silently Absorbs Excess `msg.value` Into Protocol Treasury Without Refund — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol`'s `requestHelper` function does not refund excess `msg.value` to the caller. Any amount above `providerFee` is credited entirely to `_state.accruedPythFeesInWei` (Pyth's treasury). Because there is no "maximum fee" guard parameter, a user who queries the fee off-chain and then submits a transaction can silently lose funds if the provider's fee decreases between the query and execution — a realistic MEV/transaction-ordering scenario.

---

### Finding Description

In `requestHelper`, the fee accounting is:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The Pyth treasury receives `msg.value - providerFee`, **not** the fixed `pythFeeInWei`. Any excess `msg.value` beyond `requiredFee` is silently absorbed into `accruedPythFeesInWei` and is never returned to the caller.

The public interface explicitly acknowledges this:

> "Note that excess value is *not* refunded to the caller." [2](#0-1) [3](#0-2) 

None of the request entry points (`request`, `requestWithCallback`, `requestV2`) accept a "maximum acceptable fee" parameter that would let callers protect themselves: [4](#0-3) [5](#0-4) 

The test suite even explicitly exercises the overpayment path and confirms the excess goes to the Pyth treasury: [6](#0-5) 

---

### Impact Explanation

**Scenario (MEV / transaction ordering):**

1. User calls `getFeeV2(provider, gasLimit)` off-chain → returns 100 wei (`providerFee=80`, `pythFeeInWei=20`).
2. User submits transaction with `msg.value = 100 wei`.
3. A validator (or the provider itself) inserts `setProviderFee(40 wei)` before the user's transaction.
4. User's transaction executes: `requiredFee = 60 wei`, `msg.value = 100 wei` → **succeeds**.
5. Provider receives 40 wei; Pyth treasury receives 60 wei (100 − 40).
6. User paid 100 wei for a service priced at 60 wei at execution time — **40 wei lost with no recourse**.

The user cannot protect themselves because there is no slippage/max-fee guard. The Frax analog had a `deadline` parameter; Entropy has no equivalent. [7](#0-6) 

---

### Likelihood Explanation

- Providers can call `setProviderFee` at any time with no time-lock.
- Any block producer or MEV searcher can reorder a `setProviderFee` call ahead of a pending `request*` call.
- Users commonly add a small buffer to `msg.value` to avoid reverts from fee increases, making overpayment the default safe pattern — yet the contract silently keeps the buffer.
- The test suite confirms this is the live behavior, not a hypothetical. [8](#0-7) 

---

### Recommendation

Add a `maxFee` parameter to all `request*` entry points and revert if `msg.value > maxFee`, or refund the excess:

```solidity
// Option A: refund excess
if (msg.value > requiredFee) {
    (bool ok,) = msg.sender.call{value: msg.value - requiredFee}("");
    require(ok, "refund failed");
}
_state.accruedPythFeesInWei += _state.pythFeeInWei;

// Option B: add maxFee guard
function requestV2(address provider, bytes32 userRandomNumber, uint32 gasLimit, uint128 maxFee)
    external payable returns (uint64) {
    if (msg.value > maxFee) revert FeeTooHigh();
    ...
}
```

---

### Proof of Concept

```solidity
function testOverpayGoesToPythTreasury() public {
    // User queries fee = 100 wei
    uint128 fee = random.getFeeV2(provider1, 0); // e.g. 100 wei

    // Provider front-runs: decreases fee to 50 wei
    vm.prank(provider1);
    random.setProviderFee(30); // pythFee=20, providerFee=30 → total=50

    // User's tx executes with original msg.value
    vm.deal(user1, fee);
    vm.prank(user1);
    random.requestV2{value: fee}(provider1, bytes32(uint256(42)), 0);

    // Provider only got 30 wei; Pyth treasury got 70 wei (100-30)
    // User paid 100 wei for a 50-wei service
    assertEq(random.getProviderInfoV2(provider1).accruedFeesInWei, 30);
    assertEq(random.getAccruedPythFees(), fee - 30); // 70 wei absorbed
}
``` [1](#0-0) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L214-240)
```text
    function requestHelper(
        address provider,
        bytes32 userCommitment,
        bool useBlockhash,
        bool isRequestWithCallback,
        uint32 callbackGasLimit
    ) internal returns (EntropyStructsV2.Request storage req) {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (_state.providers[provider].sequenceNumber == 0)
            revert EntropyErrors.NoSuchProvider();

        // Assign a sequence number to the request
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;

        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);

```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L322-336)
```text
    function request(
        address provider,
        bytes32 userCommitment,
        bool useBlockHash
    ) public payable override returns (uint64 assignedSequenceNumber) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            userCommitment,
            useBlockHash,
            false,
            0
        );
        assignedSequenceNumber = req.sequenceNumber;
        emit Requested(EntropyStructConverter.toV1Request(req));
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L809-827)
```text
    // Set provider fee. It will revert if provider is not registered.
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L66-67)
```text
    // This method will revert unless the caller provides a sufficient fee (at least `getFee(provider)`) as msg.value.
    // Note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L94-96)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L682-699)
```text
        // this call overpays for the random number
        requestWithFee(
            user2,
            pythFeeInWei + provider2FeeInWei + 10000,
            provider2,
            42,
            false
        );

        assertEq(
            random.getProviderInfoV2(provider1).accruedFeesInWei,
            provider1FeeInWei * 3
        );
        assertEq(
            random.getProviderInfoV2(provider2).accruedFeesInWei,
            provider2FeeInWei * 2
        );
        assertEq(random.getAccruedPythFees(), pythFeeInWei * 5 + 10000);
```
