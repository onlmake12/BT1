### Title
`Executor` Contract Silently Accepts and Permanently Traps ETH Without Unprivileged Recovery Path — (File: `target_chains/ethereum/contracts/contracts/executor/Executor.sol`)

---

### Summary

`Executor.sol` declares `receive() external payable {}` at line 113, silently accepting native ETH from any sender. Neither `Executor.sol` nor `ExecutorUpgradable.sol` exposes any `withdraw`, `rescueEth`, or equivalent function. The only mechanism that can move ETH out of the contract is the governance-gated `execute()` function, which requires a valid Wormhole VAA signed by the authorized owner emitter. ETH sent directly to the contract by any unprivileged party is effectively trapped until a privileged governance action is crafted and executed.

---

### Finding Description

`Executor.sol` is the on-chain governance execution engine for Pyth. It inherits into `ExecutorUpgradable.sol` and is deployed as a UUPS proxy. The contract has two ETH-accepting entry points:

1. **`execute(bytes memory encodedVm) public payable`** — the governance execution path. It can forward ETH to a target contract via `address(callAddress).call{value: gi.value}(gi.callData)`, where `gi.value` is encoded inside the governance VAA payload.

2. **`receive() external payable {}`** — a bare, unconditional ETH receiver with no logic. [1](#0-0) 

The comment on line 112 ("Called when `msg.value` is not zero and the call data is empty") confirms this is intentional, but no corresponding withdrawal path exists for unprivileged callers. [2](#0-1) 

A search across all executor-scope Solidity files confirms there is no `withdraw`, `rescueEth`, `recoverEth`, or `sweepEth` function anywhere in the executor module.

ETH accumulates in the contract from:
- Direct plain-ETH transfers via `receive()`
- Excess `msg.value` passed to `execute()` when `msg.value > gi.value`

The only recovery path is for Pyth governance to craft a new Wormhole VAA with `gi.value = <amount>` pointing to a recipient address. This is a privileged, off-chain, multi-step operation — not available to any unprivileged user.

---

### Impact Explanation

Any ETH sent to the `Executor` contract — whether by accident, by a user mistaking it for a payable endpoint, or by a keeper overpaying `execute()` — is trapped until governance explicitly acts to recover it. If governance does not craft a recovery VAA, the ETH is permanently locked. This is a direct analog to the BathBuddy finding: a contract that accepts ETH with no unprivileged withdrawal path.

**Impact:** Medium — ETH funds locked in a production governance contract, recoverable only via a privileged governance action that may never occur for small or accidental deposits.

---

### Likelihood Explanation

The `execute()` function is `payable` and is called by relayers/keepers who submit governance VAAs. A keeper could easily overpay `msg.value` relative to `gi.value` encoded in the VAA, leaving a residual balance. Additionally, any party can send ETH directly via `receive()`. Both paths are reachable by unprivileged actors with no special access.

---

### Recommendation

Either:

1. **Remove `receive()`** if the contract is not intended to hold ETH independently of governance calls. The `execute()` function is already `payable` and can receive ETH for forwarding in the same transaction.

2. **Add a governance-only ETH rescue function** that allows the owner (or a governance VAA) to sweep the contract's ETH balance to a designated address, making the recovery path explicit and auditable.

3. **Add a refund mechanism** in `execute()` that returns `msg.value - gi.value` to the caller when `msg.value > gi.value`, preventing residual ETH accumulation.

---

### Proof of Concept

```
1. Any address calls:
   (bool ok,) = address(executor).call{value: 1 ether}("");
   // Succeeds silently via receive() external payable {}

2. executor.balance == 1 ether

3. No function on Executor or ExecutorUpgradable allows
   an unprivileged caller to retrieve this ETH.

4. Recovery requires Pyth governance to publish a Wormhole VAA
   encoding gi.value = 1 ether and gi.callAddress = some recipient,
   then call executor.execute(encodedVm).
``` [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L66-113)
```text
    function execute(
        bytes memory encodedVm
    ) public payable returns (bytes memory response) {
        IWormhole.VM memory vm = verifyGovernanceVM(encodedVm);

        GovernanceInstruction memory gi = parseGovernanceInstruction(
            vm.payload
        );

        if (gi.targetChainId != chainId && gi.targetChainId != 0)
            revert ExecutorErrors.InvalidGovernanceTarget();

        if (
            gi.action != ExecutorAction.Execute ||
            gi.executorAddress != address(this)
        ) revert ExecutorErrors.DeserializationError();

        // Check if the gi.callAddress is a contract account.
        uint len;
        address callAddress = address(gi.callAddress);
        assembly {
            len := extcodesize(callAddress)
        }
        if (len == 0) revert ExecutorErrors.InvalidContractTarget();

        bool success;
        (success, response) = address(callAddress).call{value: gi.value}(
            gi.callData
        );

        // Check if the call was successful or not.
        if (!success) {
            // If there is return data, the delegate call reverted with a reason or a custom error, which we bubble up.
            if (response.length > 0) {
                // The first word of response is the length, so when we call revert we add 1 word (32 bytes)
                // to give the pointer to the beginning of the revert data and pass the size as the second argument.
                assembly {
                    let returndata_size := mload(response)
                    revert(add(32, response), returndata_size)
                }
            } else {
                revert ExecutorErrors.ExecutionReverted();
            }
        }
    }

    /// @dev Called when `msg.value` is not zero and the call data is empty.
    receive() external payable {}
```

**File:** target_chains/ethereum/contracts/contracts/executor/ExecutorUpgradable.sol (L1-99)
```text
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";

import "./Executor.sol";
import "./ExecutorErrors.sol";

contract ExecutorUpgradable is
    Initializable,
    Ownable2StepUpgradeable,
    UUPSUpgradeable,
    Executor
{
    event ContractUpgraded(
        address oldImplementation,
        address newImplementation
    );

    function initialize(
        address wormhole,
        uint64 lastExecutedSequence,
        uint16 chainId,
        uint16 ownerEmitterChainId,
        bytes32 ownerEmitterAddress
    ) public initializer {
        require(wormhole != address(0), "wormhole is zero address");

        __Ownable_init();
        __UUPSUpgradeable_init();

        Executor._initialize(
            wormhole,
            lastExecutedSequence,
            chainId,
            ownerEmitterChainId,
            ownerEmitterAddress
        );

        // Transfer ownership to the contract itself.
        _transferOwnership(address(this));
    }

    /// Ensures the contract cannot be uninitialized and taken over.
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() initializer {}

    // Only allow the owner to upgrade the proxy to a new implementation.
    function _authorizeUpgrade(address) internal override onlyOwner {}

    // Upgrade the contract to the given newImplementation. The `newImplementation`
    // should implement the method  `entropyUpgradableMagic`, see below. If the method
    // is not implemented or if the magic is different from the current contract, this call
    // will revert.
    function upgradeTo(address newImplementation) external override onlyProxy {
        address oldImplementation = _getImplementation();
        _authorizeUpgrade(newImplementation);
        _upgradeToAndCallUUPS(newImplementation, new bytes(0), false);

        magicCheck();

        emit ContractUpgraded(oldImplementation, _getImplementation());
    }

    // Upgrade the contract to the given newImplementation and call it with the given data.
    // The `newImplementation` should implement the method  `entropyUpgradableMagic`, see
    // below. If the method is not implemented or if the magic is different from the current
    // contract, this call will revert.
    function upgradeToAndCall(
        address newImplementation,
        bytes memory data
    ) external payable override onlyProxy {
        address oldImplementation = _getImplementation();
        _authorizeUpgrade(newImplementation);
        _upgradeToAndCallUUPS(newImplementation, data, true);

        magicCheck();

        emit ContractUpgraded(oldImplementation, _getImplementation());
    }

    function magicCheck() internal view {
        // Calling a method using `this.<method>` will cause a contract call that will use
        // the new contract. This call will fail if the method does not exists or the magic
        // is different.
        if (this.entropyUpgradableMagic() != 0x66697288)
            revert ExecutorErrors.InvalidMagicValue();
    }

    function entropyUpgradableMagic() public pure returns (uint32) {
        return 0x66697288;
    }

    function version() public pure returns (string memory) {
        return "0.1.1";
    }
}
```
