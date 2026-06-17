### Title
`setFeeManager()` Can Be Front-Run by the Outgoing Fee Manager to Drain All Accrued Provider Fees — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `setFeeManager` function in `Entropy.sol` (and identically in `Echo.sol`) suffers from the same race condition class as the ERC20 `approve()` vulnerability. When a provider calls `setFeeManager(newManager)` to rotate or revoke their fee manager, the outgoing fee manager can observe the pending transaction in the mempool and front-run it with `withdrawAsFeeManager(provider, fullBalance)`, draining all accrued fees before the manager change takes effect.

---

### Finding Description

`setFeeManager` atomically overwrites `provider.feeManager` with no two-step delay or time-lock:

```solidity
// Entropy.sol L876-893
function setFeeManager(address manager) external override {
    ...
    address oldFeeManager = provider.feeManager;
    provider.feeManager = manager;          // immediate, single-tx overwrite
    emit ProviderFeeManagerUpdated(...);
}
```

`withdrawAsFeeManager` only checks that `msg.sender == providerInfo.feeManager` at call time, then immediately transfers the full requested amount:

```solidity
// Entropy.sol L175-209
function withdrawAsFeeManager(address provider, uint128 amount) external override {
    ...
    if (providerInfo.feeManager != msg.sender) revert EntropyErrors.Unauthorized();
    ...
    providerInfo.accruedFeesInWei -= amount;
    (bool sent, ) = msg.sender.call{value: amount}("");
    ...
}
```

The same pattern is present in `Echo.sol` at lines 350–379.

There is no mechanism to atomically revoke the old manager's access and prevent a withdrawal in the same transaction. The window between the provider broadcasting `setFeeManager(newManager)` and that transaction being mined is exploitable.

---

### Impact Explanation

A malicious or compromised outgoing fee manager can steal the provider's entire `accruedFeesInWei` balance. The provider loses all fees earned up to the point of the manager rotation. The stolen funds are native ETH (or the chain's native token) sent directly to the attacker's address. There is no recovery path once the withdrawal succeeds.

---

### Likelihood Explanation

Medium. The attack requires the outgoing fee manager to be adversarial — but this is precisely the scenario that motivates a provider to call `setFeeManager` in the first place (key compromise, business relationship change, or security rotation). Any provider who has ever delegated fee management to a third-party keeper (as the Fortuna keeper infrastructure does via `withdraw_as_fee_manager`) and later wishes to revoke that delegation is exposed. The Fortuna off-chain keeper code (`apps/fortuna/src/keeper/fee.rs`) actively monitors and calls `withdrawAsFeeManager` on a recurring basis, making the front-run trivially automatable.

---

### Recommendation

Implement a two-step fee manager rotation with a time-lock, analogous to the `increaseAllowance`/`decreaseAllowance` pattern recommended in the original report:

1. **Pending manager pattern**: Introduce a `pendingFeeManager` field. `setFeeManager` sets `pendingFeeManager` but does not immediately overwrite `feeManager`. A separate `acceptFeeManager` call (by the new manager) finalizes the change after a delay.
2. **Alternatively**: Allow the provider to call `setFeeManager(address(0))` to immediately disable the current fee manager (zero-ing out the role), then set a new one in a second transaction. This limits the race window to the disable step, where there are no funds at risk from the new manager.
3. **Emit a warning event** on `setFeeManager` so off-chain monitoring can detect suspicious same-block withdrawals.

---

### Proof of Concept

1. Provider `P` has `accruedFeesInWei = 10 ETH` and `feeManager = oldManager`.
2. `P` submits `setFeeManager(newManager)` with standard gas price.
3. `oldManager` monitors the mempool, detects the pending transaction.
4. `oldManager` submits `withdrawAsFeeManager(P, 10 ETH)` with a higher gas price (front-run).
5. `oldManager`'s transaction is mined first: `providerInfo.accruedFeesInWei` becomes 0, and 10 ETH is transferred to `oldManager`.
6. `P`'s `setFeeManager(newManager)` is mined next: `feeManager` is updated to `newManager`, but the 10 ETH is already gone.
7. `newManager` inherits a zero balance; `P` has permanently lost 10 ETH.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L350-379)
```text
    function setFeeManager(address manager) external override {
        require(
            _state.providers[msg.sender].isRegistered,
            "Provider not registered"
        );
        address oldFeeManager = _state.providers[msg.sender].feeManager;
        _state.providers[msg.sender].feeManager = manager;
        emit FeeManagerUpdated(msg.sender, oldFeeManager, manager);
    }

    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```
