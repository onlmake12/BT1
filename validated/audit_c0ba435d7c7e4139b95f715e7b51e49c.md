### Title
Fee Manager Front-Running on `setFeeManager` Allows Draining of Accrued Provider Fees - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

When a provider calls `setFeeManager` to replace their current fee manager, the outgoing fee manager can observe the pending transaction in the mempool and front-run it with `withdrawAsFeeManager`, draining all accrued provider fees before the replacement takes effect. This is a direct structural analog to the ERC-20 approve/transferFrom double-spend: the "allowance" is the fee manager role, and the "transfer" is the withdrawal of accrued ETH fees.

---

### Finding Description

`setFeeManager` in `Entropy.sol` performs an immediate, single-step replacement of the fee manager address with no timelock, pending-state, or two-step confirmation: [1](#0-0) 

The fee manager role grants the ability to withdraw any amount of the provider's accrued fees via `withdrawAsFeeManager`: [2](#0-1) 

The authorization check is a single storage read against the current `feeManager` field: [3](#0-2) 

Because `setFeeManager` overwrites `provider.feeManager` atomically with no intermediate state, the window between "old manager is still authorized" and "new manager takes over" is exploitable by any mempool observer who holds the old fee manager key.

The same pattern exists in `Echo.sol`: [4](#0-3) 

---

### Impact Explanation

A provider who attempts to rotate their fee manager (e.g., due to suspected key compromise, or a change in business relationship) will lose their entire `accruedFeesInWei` balance. The outgoing fee manager can drain 100% of the accrued ETH before the replacement transaction is mined. The provider has no recourse: the withdrawal is legitimate under the contract's current rules at the time it executes.

---

### Likelihood Explanation

The scenario is realistic. Providers are expected to rotate fee managers over time (the interface explicitly documents the role as replaceable). Any provider operating on a public EVM chain with a visible mempool is exposed. The attacker only needs to monitor for a `setFeeManager` call targeting their address and submit a `withdrawAsFeeManager` with a higher gas price. No special capability beyond holding the current fee manager key is required.

---

### Recommendation

Replace the single-step `setFeeManager` with a two-step commit-accept pattern (analogous to OpenZeppelin `Ownable2Step`):

1. Provider calls `proposeFeeManager(address newManager)` — stores `pendingFeeManager` but does **not** revoke the current manager.
2. `newManager` calls `acceptFeeManager()` — only then does `feeManager` update.

This ensures the old manager is never replaced until the new manager has explicitly accepted, eliminating the front-running window. Alternatively, add a time-delayed transition where the old manager's withdrawal rights are revoked at the moment `proposeFeeManager` is called, but the new manager cannot act until the delay expires.

---

### Proof of Concept

```
1. Provider registers; feeManager = A.
2. Users make entropy requests; provider accrues X wei in accruedFeesInWei.
3. Provider broadcasts setFeeManager(B) (e.g., to rotate after a suspected key leak).
4. A observes the pending tx in the mempool.
5. A broadcasts withdrawAsFeeManager(provider, X) with higher gas — mines first.
   → providerInfo.accruedFeesInWei becomes 0; X wei sent to A.
6. Provider's setFeeManager(B) mines; feeManager = B.
7. B calls withdrawAsFeeManager(provider, ...) — balance is 0; all fees are gone.
```

The structural parallel to ERC-20 EIP-738:
- ERC-20: Alice changes Bob's allowance N→M; Bob front-runs to spend N, then spends M (N+M total).
- Entropy: Provider changes fee manager A→B; A front-runs to withdraw all X wei, then B inherits an empty balance (A gets X, B gets 0). [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L175-209)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
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

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(provider, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            provider,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L876-893)
```text
    function setFeeManager(address manager) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];
        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        address oldFeeManager = provider.feeManager;
        provider.feeManager = manager;
        emit ProviderFeeManagerUpdated(msg.sender, oldFeeManager, manager);
        emit EntropyEventsV2.ProviderFeeManagerUpdated(
            msg.sender,
            oldFeeManager,
            manager,
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
