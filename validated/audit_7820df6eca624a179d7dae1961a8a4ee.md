### Title
Malicious Admin Can Front-Run Owner's `proposeAdmin` to Permanently Retain Admin Access - (File: `target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol`)

---

### Summary

In `EntropyGovernance.sol` (and identically in `SchedulerGovernance.sol`), the `proposeAdmin` function is callable by **either the owner or the current admin**. Because `proposeAdmin` unconditionally overwrites any pending proposed admin, a malicious or compromised admin can front-run the owner's attempt to replace them by nominating an attacker-controlled address, then immediately accepting the admin role — permanently retaining admin-level access even after the owner's replacement transaction lands.

---

### Finding Description

`EntropyGovernance.proposeAdmin` is gated by `_authoriseAdminAction()`, which in `EntropyUpgradable` is implemented as:

```solidity
function _authoriseAdminAction() internal view override {
    if (msg.sender != owner() && msg.sender != _state.admin)
        revert EntropyErrors.Unauthorized();
}
``` [1](#0-0) 

This means the current admin can call `proposeAdmin(attackerAddress)` at any time, including in the same block as the owner's `proposeAdmin(newLegitAdmin)` call. The function unconditionally overwrites `_state.proposedAdmin`:

```solidity
function proposeAdmin(address newAdmin) public virtual {
    require(newAdmin != address(0), "newAdmin is zero address");
    _authoriseAdminAction();
    _state.proposedAdmin = newAdmin;
    emit NewAdminProposed(_state.admin, newAdmin);
}
``` [2](#0-1) 

`acceptAdmin` then allows whoever is `_state.proposedAdmin` to atomically become the new admin:

```solidity
function acceptAdmin() external {
    if (msg.sender != _state.proposedAdmin)
        revert EntropyErrors.Unauthorized();
    address oldAdmin = _state.admin;
    _state.admin = msg.sender;
    _state.proposedAdmin = address(0);
    emit NewAdminAccepted(oldAdmin, msg.sender);
}
``` [3](#0-2) 

The identical pattern exists in `SchedulerGovernance.sol`: [4](#0-3) 

with the same `_authorizeAdminAction` check in `SchedulerUpgradeable`: [5](#0-4) 

---

### Impact Explanation

A malicious or compromised admin retains the ability to:

- Call `setPythFee` to set an arbitrarily high protocol fee, extracting value from all Entropy users. [6](#0-5) 
- Call `withdrawFee` to drain all accrued Pyth fees to an attacker-controlled address. [7](#0-6) 
- Call `setDefaultProvider` to redirect users to a malicious randomness provider. [8](#0-7) 

The owner cannot effectively revoke admin access because the admin can always front-run any `proposeAdmin` call with their own, installing a fresh attacker-controlled address as admin before the owner's transaction is mined. The owner has no atomic "remove admin" primitive — only `proposeAdmin` + `acceptAdmin`, both of which the current admin can race against.

---

### Likelihood Explanation

Likelihood is **Low**. This requires the admin key to be compromised or the admin to act maliciously. However, the scenario is realistic: admin keys can be leaked, and the admin role is intentionally separated from the owner role precisely because it is considered a lower-trust position. The owner's inability to atomically revoke a compromised admin is a meaningful gap. The attack requires only standard EVM front-running (mempool observation + higher gas), which is trivially achievable on any public EVM chain where these contracts are deployed.

---

### Recommendation

Restrict `proposeAdmin` so that only the **owner** (not the current admin) can nominate a new admin. The current admin's ability to self-perpetuate via `proposeAdmin` is the root cause. Alternatively, add an owner-only `forceSetAdmin(address)` that bypasses the two-step flow and atomically replaces the admin without the current admin's cooperation.

---

### Proof of Concept

1. Contracts are deployed with `owner = O`, `admin = A` (malicious/compromised).
2. `O` observes that `A` is malicious and submits `proposeAdmin(B)` (where `B` is a trusted new admin).
3. `A` monitors the mempool, sees `O`'s pending transaction, and front-runs it with `proposeAdmin(A2)` (where `A2` is another attacker-controlled address), using higher gas.
4. `A2` immediately calls `acceptAdmin()` in the same block — `_state.admin` is now `A2`.
5. `O`'s `proposeAdmin(B)` lands: `_state.proposedAdmin = B`. But `_state.admin` is already `A2`.
6. `B` calls `acceptAdmin()` — `_state.admin` becomes `B`. But `A2` (still under attacker control) can immediately repeat step 3, calling `proposeAdmin(A3)` and having `A3` call `acceptAdmin()`.
7. The attacker can repeat this indefinitely, always racing the owner's replacement attempts, retaining admin access and continuing to call `withdrawFee`, `setPythFee`, and `setDefaultProvider`.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L63-66)
```text
    function _authoriseAdminAction() internal view override {
        if (msg.sender != owner() && msg.sender != _state.admin)
            revert EntropyErrors.Unauthorized();
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L33-40)
```text
    function proposeAdmin(address newAdmin) public virtual {
        require(newAdmin != address(0), "newAdmin is zero address");

        _authoriseAdminAction();

        _state.proposedAdmin = newAdmin;
        emit NewAdminProposed(_state.admin, newAdmin);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L45-54)
```text
    function acceptAdmin() external {
        if (msg.sender != _state.proposedAdmin)
            revert EntropyErrors.Unauthorized();

        address oldAdmin = _state.admin;
        _state.admin = msg.sender;

        _state.proposedAdmin = address(0);
        emit NewAdminAccepted(oldAdmin, msg.sender);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L67-74)
```text
    function setPythFee(uint128 newPythFee) external {
        _authoriseAdminAction();

        uint oldPythFee = _state.pythFeeInWei;
        _state.pythFeeInWei = newPythFee;

        emit PythFeeSet(oldPythFee, newPythFee);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L83-94)
```text
    function setDefaultProvider(address newDefaultProvider) external {
        require(
            newDefaultProvider != address(0),
            "newDefaultProvider is zero address"
        );
        _authoriseAdminAction();

        address oldDefaultProvider = _state.defaultProvider;
        _state.defaultProvider = newDefaultProvider;

        emit DefaultProviderSet(oldDefaultProvider, newDefaultProvider);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L103-116)
```text
    function withdrawFee(address targetAddress, uint128 amount) external {
        require(targetAddress != address(0), "targetAddress is zero address");
        _authoriseAdminAction();

        if (amount > _state.accruedPythFeesInWei)
            revert EntropyErrors.InsufficientFee();

        _state.accruedPythFeesInWei -= amount;

        (bool success, ) = targetAddress.call{value: amount}("");
        require(success, "Failed to withdraw fees");

        emit FeeWithdrawn(targetAddress, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L34-55)
```text
    function proposeAdmin(address newAdmin) public virtual {
        require(newAdmin != address(0), "newAdmin is zero address");

        _authorizeAdminAction();

        _state.proposedAdmin = newAdmin;
        emit NewAdminProposed(_state.admin, newAdmin);
    }

    /**
     * @dev The proposed admin accepts the admin transfer.
     */
    function acceptAdmin() external {
        if (msg.sender != _state.proposedAdmin)
            revert SchedulerErrors.Unauthorized();

        address oldAdmin = _state.admin;
        _state.admin = msg.sender;

        _state.proposedAdmin = address(0);
        emit NewAdminAccepted(oldAdmin, msg.sender);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerUpgradeable.sol (L57-60)
```text
    function _authorizeAdminAction() internal view override {
        if (msg.sender != owner() && msg.sender != _state.admin)
            revert SchedulerErrors.Unauthorized();
    }
```
