### Title
Single-Step Ownership Transfer in `PythLazer` Permanently Locks Critical Owner Functions - (`File: lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer` inherits from `OwnableUpgradeable` (single-step ownership transfer) instead of `Ownable2StepUpgradeable`. Every other upgradeable Pyth contract (`EntropyUpgradable`, `ExecutorUpgradable`, `EchoUpgradeable`, `SchedulerUpgradeable`) already uses `Ownable2StepUpgradeable`. A single erroneous `transferOwnership()` call permanently and irrecoverably locks all `onlyOwner` functions in `PythLazer`, including trusted-signer management and contract upgrades.

### Finding Description
`PythLazer` is the on-chain EVM contract for the Pyth Lazer price-feed product. It is deployed as a UUPS proxy and its owner controls two critical capabilities:

1. `updateTrustedSigner(address, uint256)` — adds, updates, or removes the set of ECDSA signers whose signatures are accepted by `verifyUpdate()`. Every Lazer price-feed consumer depends on at least one valid, non-expired signer being present.
2. `_authorizeUpgrade(address)` — the sole gate for UUPS upgrades; without it no bug fix or feature upgrade can ever be applied.

The contract inherits `OwnableUpgradeable`:

```solidity
// lazer/contracts/evm/src/PythLazer.sol
import "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";

contract PythLazer is OwnableUpgradeable, UUPSUpgradeable {
``` [1](#0-0) 

`OwnableUpgradeable.transferOwnership()` immediately sets `_owner` to the supplied address with no confirmation step. If the caller supplies an incorrect address (e.g. a typo, an undeployed contract, or a wrong chain address), ownership is irrecoverably lost in a single transaction.

By contrast, every other Pyth upgradeable contract uses `Ownable2StepUpgradeable`, which requires the new owner to call `acceptOwnership()` before the transfer completes:

- `EntropyUpgradable` — `Ownable2StepUpgradeable` [2](#0-1) 
- `ExecutorUpgradable` — `Ownable2StepUpgradeable` [3](#0-2) 
- `EchoUpgradeable` — `Ownable2StepUpgradeable` [4](#0-3) 
- `SchedulerUpgradeable` — `Ownable2StepUpgradeable` [5](#0-4) 

`PythLazer` is the only production Pyth EVM contract that still uses the unsafe single-step variant.

The deployment script `PythLazerChangeOwnership.s.sol` confirms that ownership transfer is a real operational action that is performed post-deployment:

```solidity
lazer.transferOwnership(NEW_OWNER);
``` [6](#0-5) 

### Impact Explanation
If ownership is transferred to an uncontrolled address:

- `updateTrustedSigner` becomes permanently uncallable. All currently registered signers have an `expiresAt` timestamp. Once every signer expires, `isValidSigner()` returns `false` for all addresses and every call to `verifyUpdate()` reverts with `"invalid signer"`. All Lazer price-feed consumers on that chain are permanently bricked with no recovery path.
- `_authorizeUpgrade` becomes permanently uncallable, making the proxy non-upgradeable. No security patch or migration can ever be applied. [7](#0-6) [8](#0-7) 

### Likelihood Explanation
The ownership transfer is a documented, scripted operational step. The script reads `NEW_OWNER` from an environment variable at runtime, which is a common source of human error (wrong chain, undeployed contract, copy-paste mistake). The error may not be noticed immediately; it only becomes apparent when a signer needs to be rotated or the contract needs to be upgraded, by which time the state is already irrecoverable.

### Recommendation
Replace `OwnableUpgradeable` with `Ownable2StepUpgradeable`, consistent with every other Pyth upgradeable contract:

```solidity
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";

contract PythLazer is Ownable2StepUpgradeable, UUPSUpgradeable {
```

This requires the new owner to call `acceptOwnership()` before the transfer is finalised, providing a recoverable window if an incorrect address is used.

### Proof of Concept
1. Current owner calls `lazer.transferOwnership(wrongAddress)` — ownership is immediately and irrevocably transferred.
2. All signers in `trustedSigners[]` have finite `expiresAt` values. Once they expire, `isValidSigner()` returns `false`.
3. Any call to `verifyUpdate()` reverts at `require(!isValidSigner(signer))`.
4. No one can call `updateTrustedSigner()` to add a new signer (reverts with `OwnableUnauthorizedAccount`).
5. No one can call `upgradeTo()` to deploy a fixed implementation (reverts via `_authorizeUpgrade → onlyOwner`).
6. The contract and all its consumers are permanently non-functional on that chain. [9](#0-8)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L5-8)
```text
import "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

contract PythLazer is OwnableUpgradeable, UUPSUpgradeable {
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L29-34)
```text
    function _authorizeUpgrade(address) internal override onlyOwner {}

    function updateTrustedSigner(
        address trustedSigner,
        uint256 expiresAt
    ) external onlyOwner {
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L100-106)
```text
        if (signer == address(0)) {
            revert("invalid signature");
        }
        if (!isValidSigner(signer)) {
            revert("invalid signer");
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L6-14)
```text
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import "@pythnetwork/entropy-sdk-solidity/EntropyErrors.sol";

import "./EntropyGovernance.sol";
import "./Entropy.sol";

contract EntropyUpgradable is
    Initializable,
    Ownable2StepUpgradeable,
```

**File:** target_chains/ethereum/contracts/contracts/executor/ExecutorUpgradable.sol (L6-13)
```text
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";

import "./Executor.sol";
import "./ExecutorErrors.sol";

contract ExecutorUpgradable is
    Initializable,
    Ownable2StepUpgradeable,
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoUpgradeable.sol (L7-14)
```text
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import "./Echo.sol";

contract EchoUpgradeable is
    Initializable,
    Ownable2StepUpgradeable,
    UUPSUpgradeable,
    Echo
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerUpgradeable.sol (L1-10)
```text
// SPDX-License-Identifier: Apache 2

pragma solidity ^0.8.0;

import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import "./Scheduler.sol";
import "./SchedulerGovernance.sol";
import "@pythnetwork/pulse-sdk-solidity/SchedulerErrors.sol";
```

**File:** lazer/contracts/evm/script/PythLazerChangeOwnership.s.sol (L57-59)
```text
        // Transfer ownership to the new owner (executor contract)
        lazer.transferOwnership(NEW_OWNER);
        console.log("Ownership transferred");
```
