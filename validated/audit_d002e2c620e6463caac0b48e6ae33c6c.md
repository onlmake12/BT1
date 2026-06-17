### Title
Missing Admin Rotation Mechanism in `EchoUpgradeable` Permanently Locks Protocol Fee Withdrawal — (`target_chains/ethereum/contracts/contracts/echo/EchoUpgradeable.sol`)

---

### Summary

`EchoUpgradeable` sets an `admin` address at initialization that can never be changed through any governance mechanism. The `withdrawFees()` function in `Echo.sol` exclusively allows `msg.sender == _state.admin` to withdraw accumulated Pyth protocol fees, and sends those fees directly to `msg.sender`. Unlike the analogous `EntropyUpgradeable` and `SchedulerUpgradeable` contracts — which both inherit governance contracts providing `proposeAdmin`/`acceptAdmin` — `EchoUpgradeable` provides no admin rotation mechanism whatsoever. The `owner` (who holds full upgrade authority) has no path to call `withdrawFees()` or to change `_state.admin`. If the admin address becomes inaccessible, all accumulated Pyth protocol fees are permanently locked in the contract.

---

### Finding Description

`EchoUpgradeable` inherits from `Ownable2StepUpgradeable` (giving it an `owner`) and from `Echo` (giving it an `admin` stored in `_state.admin`). These are two distinct roles set at initialization: [1](#0-0) 

The `withdrawFees()` function in `Echo.sol` enforces a hard check that only `_state.admin` can call it, and critically, sends the ETH directly to `msg.sender` (the admin): [2](#0-1) 

`EchoUpgradeable` does **not** inherit any governance contract. It has no `proposeAdmin`, `acceptAdmin`, or `setAdmin` function. The full function set added by `EchoUpgradeable` is only: `initialize`, `_authorizeUpgrade`, `upgradeTo`, `upgradeToAndCall`, and `version`: [3](#0-2) 

Contrast this with `EntropyUpgradeable`, which inherits `EntropyGovernance` providing `proposeAdmin`/`acceptAdmin`: [4](#0-3) 

And `SchedulerUpgradeable`, which inherits `SchedulerGovernance` providing the same: [5](#0-4) 

`SchedulerGovernance` and `EntropyGovernance` both expose `proposeAdmin`/`acceptAdmin`: [6](#0-5) 

`EchoUpgradeable` has no equivalent. The `owner` cannot call `withdrawFees()` (only `admin` can), and the `owner` has no path to change `_state.admin`. The admin role is permanently fixed at deployment.

---

### Impact Explanation

All Pyth protocol fees collected by the `Echo` contract accumulate in `_state.accruedFeesInWei`. The only withdrawal path is `withdrawFees()`, which requires `msg.sender == _state.admin` and sends ETH to `msg.sender`. If the admin address becomes inaccessible (key loss, admin contract self-destructed, admin contract lacking a `receive()` function, etc.), the entire accumulated fee balance is permanently locked. The `owner`, despite having full upgrade authority, has no direct path to recover these funds without deploying a new implementation — a non-standard emergency workaround that is not part of the intended governance design. [2](#0-1) 

---

### Likelihood Explanation

The admin is a privileged operational address set once at initialization. Operational key rotation is a standard practice. Without a `proposeAdmin`/`acceptAdmin` mechanism, any rotation attempt is impossible through normal governance. The probability of needing admin rotation over the contract's lifetime is non-trivial (key compromise, operational restructuring, multisig migration). The absence of this mechanism in `EchoUpgradeable` — while it is present in every other analogous Pyth upgradeable contract — indicates an oversight rather than an intentional design choice.

---

### Recommendation

Add a `proposeAdmin`/`acceptAdmin` two-step admin rotation mechanism to `EchoUpgradeable`, mirroring the pattern already implemented in `EntropyGovernance` and `SchedulerGovernance`: [7](#0-6) 

Additionally, consider allowing the `owner` to also call `withdrawFees()` (as `EntropyGovernance.withdrawFee` does via `_authoriseAdminAction` which accepts both owner and admin), and consider adding a `targetAddress` parameter to `withdrawFees()` so fees are not forced to go to `msg.sender`.

---

### Proof of Concept

1. Deploy `EchoUpgradeable` with `owner = Alice`, `admin = Bob`.
2. Users call `requestPriceUpdatesWithCallback`; fees accumulate in `_state.accruedFeesInWei`.
3. Bob's key is lost. Alice (owner) attempts to recover fees or rotate admin.
4. Alice calls `withdrawFees()` → reverts: `"Only admin can withdraw fees"`.
5. Alice searches for `proposeAdmin`/`setAdmin` → no such function exists in `EchoUpgradeable`.
6. All accumulated ETH fees are permanently locked.

The root cause is confirmed by the absence of any admin-changing function in `EchoUpgradeable`: [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/EchoUpgradeable.sol (L1-75)
```text
// SPDX-License-Identifier: Apache 2

pragma solidity ^0.8.0;

import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import "./Echo.sol";

contract EchoUpgradeable is
    Initializable,
    Ownable2StepUpgradeable,
    UUPSUpgradeable,
    Echo
{
    event ContractUpgraded(
        address oldImplementation,
        address newImplementation
    );

    function initialize(
        address owner,
        address admin,
        uint96 pythFeeInWei,
        address pythAddress,
        address defaultProvider,
        bool prefillRequestStorage,
        uint32 exclusivityPeriodSeconds
    ) external initializer {
        require(owner != address(0), "owner is zero address");
        require(admin != address(0), "admin is zero address");

        __Ownable_init();
        __UUPSUpgradeable_init();

        Echo._initialize(
            admin,
            pythFeeInWei,
            pythAddress,
            defaultProvider,
            prefillRequestStorage,
            exclusivityPeriodSeconds
        );

        _transferOwnership(owner);
    }

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() initializer {}

    function _authorizeUpgrade(address) internal override onlyOwner {}

    function upgradeTo(address newImplementation) external override onlyProxy {
        address oldImplementation = _getImplementation();
        _authorizeUpgrade(newImplementation);
        _upgradeToAndCallUUPS(newImplementation, new bytes(0), false);

        emit ContractUpgraded(oldImplementation, _getImplementation());
    }

    function upgradeToAndCall(
        address newImplementation,
        bytes memory data
    ) external payable override onlyProxy {
        address oldImplementation = _getImplementation();
        _authorizeUpgrade(newImplementation);
        _upgradeToAndCallUUPS(newImplementation, data, true);

        emit ContractUpgraded(oldImplementation, _getImplementation());
    }

    function version() public pure returns (string memory) {
        return "1.0.0";
    }
}
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-299)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L12-18)
```text
contract EntropyUpgradable is
    Initializable,
    Ownable2StepUpgradeable,
    UUPSUpgradeable,
    Entropy,
    EntropyGovernance
{
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerUpgradeable.sol (L12-18)
```text
contract SchedulerUpgradeable is
    Initializable,
    Ownable2StepUpgradeable,
    UUPSUpgradeable,
    Scheduler,
    SchedulerGovernance
{
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

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L33-58)
```text
    function proposeAdmin(address newAdmin) public virtual {
        require(newAdmin != address(0), "newAdmin is zero address");

        _authoriseAdminAction();

        _state.proposedAdmin = newAdmin;
        emit NewAdminProposed(_state.admin, newAdmin);
    }

    /**
     * @dev The proposed admin accepts the admin transfer.
     */
    function acceptAdmin() external {
        if (msg.sender != _state.proposedAdmin)
            revert EntropyErrors.Unauthorized();

        address oldAdmin = _state.admin;
        _state.admin = msg.sender;

        _state.proposedAdmin = address(0);
        emit NewAdminAccepted(oldAdmin, msg.sender);
    }

    function getAdmin() external view returns (address) {
        return _state.admin;
    }
```
