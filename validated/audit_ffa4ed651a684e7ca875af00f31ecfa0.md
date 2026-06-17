### Title
Frontrunning `setProviderFee()` Allows Users to Lock In Old Lower Fee Before Increase — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary
When an Entropy provider calls `setProviderFee()` to raise their fee, any user monitoring the mempool can frontrun that transaction with many `requestWithCallback` / `requestV2` calls at the old lower fee. The provider's `accruedFeesInWei` is credited at the stale rate for every frontrun request, causing direct revenue loss to the provider. An attacker who resells entropy services to downstream users at the new higher fee pockets the difference.

---

### Finding Description

`setProviderFee()` in `Entropy.sol` takes effect atomically in the same block with no time-lock or pending-state delay:

```solidity
// Entropy.sol line 810-827
function setProviderFee(uint128 newFeeInWei) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) revert EntropyErrors.NoSuchProvider();
    uint128 oldFeeInWei = provider.feeInWei;
    provider.feeInWei = newFeeInWei;          // ← immediate, no delay
    emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
    ...
}
```

The fee check inside `requestHelper()` reads `provider.feeInWei` at the moment of the request:

```solidity
// Entropy.sol line 233-237
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);   // reads feeInWei live
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;                 // credited at current rate
```

`getFeeV2` resolves to `provider.feeInWei + _state.pythFeeInWei` (line 764). Because there is no pending-fee queue, any request that lands before `setProviderFee` executes pays the old lower fee and credits the provider at the old rate.

The same pattern exists in `setProviderFeeAsFeeManager()` (line 829-855) and in the Echo contract's `setProviderFee()` (`Echo.sol` line 395-426), which also takes effect immediately.

---

### Impact Explanation

For every request the attacker frontrunning, the provider receives `oldFee` instead of `newFee`. The provider's revenue shortfall is:

```
loss = (newFee - oldFee) × numFrontrunRequests
```

An attacker who operates a downstream entropy-resale service (e.g., a wrapper contract that charges users the new higher fee) pockets `(newFee - oldFee)` per request. Even without resale, the attacker obtains entropy at a discount, which is a direct financial loss to the provider. The impact scales with the size of the fee increase and the number of requests the attacker can submit in a single block.

---

### Likelihood Explanation

Medium. EVM chains with public mempools (Ethereum mainnet, Polygon, BNB Chain, etc.) make pending transactions visible before inclusion. Frontrunning a single target transaction with a higher `gasPrice` / priority fee is a well-understood technique requiring no special access. The attack is most profitable when the fee increase is large (e.g., the Fortuna keeper's automated fee adjustment logic in `apps/fortuna/src/keeper/fee.rs` can raise fees significantly when the chain is active).

---

### Recommendation

Introduce a two-step fee-increase mechanism:

1. **Announce**: Provider calls `proposeProviderFee(newFee)`, storing the new fee and a `feeEffectiveAt = block.timestamp + DELAY`.
2. **Activate**: After the delay, any call to `requestHelper` (or an explicit `activateProviderFee()`) applies the new fee.

Fee *decreases* can take effect immediately (they do not create a frontrunning incentive). This mirrors the standard time-lock pattern used in DeFi to prevent fee-change frontrunning.

---

### Proof of Concept

```
Setup:
  provider.feeInWei = 1_000 wei
  pythFeeInWei      = 100 wei
  → current getFeeV2 = 1_100 wei

Step 1: Provider broadcasts setProviderFee(10_000) (10× increase).

Step 2: Attacker sees the pending tx in the mempool.

Step 3: Attacker submits 100× requestV2(provider, userRandom, 0){value: 1_100}
        with gasPrice > provider's tx, ensuring they land first.

Step 4: Each attacker request:
        - passes the fee check (msg.value 1_100 >= requiredFee 1_100)
        - credits provider.accruedFeesInWei += 1_000 (old rate)

Step 5: setProviderFee(10_000) lands. Future requests pay 10_100 wei.

Result:
  Provider expected: 100 × 10_000 = 1_000_000 wei
  Provider received: 100 ×  1_000 =   100_000 wei
  Provider loss:                      900_000 wei

  If attacker resells entropy to users at the new 10_000 rate,
  attacker profit = 100 × (10_000 − 1_000) = 900_000 wei.
```

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L760-764)
```text
    function getFeeV2(
        address provider,
        uint32 gasLimit
    ) public view override returns (uint128 feeAmount) {
        return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-426)
```text
    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
        require(
            msg.sender == provider ||
                msg.sender == _state.providers[provider].feeManager,
            "Only provider or fee manager can invoke this method"
        );

        uint96 oldBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 oldFeePerFeed = _state.providers[provider].feePerFeedInWei;
        uint96 oldFeePerGas = _state.providers[provider].feePerGasInWei;
        _state.providers[provider].baseFeeInWei = newBaseFeeInWei;
        _state.providers[provider].feePerFeedInWei = newFeePerFeedInWei;
        _state.providers[provider].feePerGasInWei = newFeePerGasInWei;
        emit ProviderFeeUpdated(
            provider,
            oldBaseFee,
            oldFeePerFeed,
            oldFeePerGas,
            newBaseFeeInWei,
            newFeePerFeedInWei,
            newFeePerGasInWei
        );
    }
```
