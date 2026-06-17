### Title
No Governance Instruction to Rotate the Owner Emitter of the EVM Executor Contract - (File: `target_chains/ethereum/contracts/contracts/executor/Executor.sol`)

---

### Summary

The `Executor.sol` contract stores a fixed governance data source (`ownerEmitterChainId` / `ownerEmitterAddress`) that authorizes which Wormhole emitter may send governance instructions to it. The `ExecutorAction` enum defines only a single action (`Execute`), with a developer `TODO` comment explicitly acknowledging the missing ability to change the governance data source. There is no governance VAA instruction that can update these owner emitter fields after deployment. If the Pyth governance multisig ever needs to rotate its Wormhole emitter address, the `Executor` contract cannot be updated through the normal cross-chain governance path.

---

### Finding Description

`Executor.sol` initializes two private state variables that define the sole authorized governance source:

```solidity
uint16 private ownerEmitterChainId;
bytes32 private ownerEmitterAddress;
```

These are set once in `_initialize` and checked in every call to `verifyGovernanceVM`:

```solidity
if (
    vm.emitterChainId != ownerEmitterChainId ||
    vm.emitterAddress != ownerEmitterAddress
) revert ExecutorErrors.UnauthorizedEmitter();
```

The only action the contract recognizes is:

```solidity
enum ExecutorAction {
    // TODO: add an instruction to change the governance data source.
    Execute // 0
}
```

The `execute()` function enforces this:

```solidity
if (
    gi.action != ExecutorAction.Execute ||
    gi.executorAddress != address(this)
) revert ExecutorErrors.DeserializationError();
```

There is no `ChangeGovernanceDataSource` action, no setter function, and no upgrade path through the governance VAA mechanism itself. The `TODO` comment in the source code confirms this gap was known but unresolved.

This is structurally identical to the Across Protocol finding: the `ForwarderBase` had `setCrossDomainAdmin` / `updateAdapter` that could only be reached via cross-chain messages, but the relay (`Router_Adapter`) provided no way to invoke them. Here, the `Executor` has `ownerEmitterChainId` / `ownerEmitterAddress` that can only be meaningfully changed via a governance VAA, but no such VAA action exists.

---

### Impact Explanation

If the Pyth governance multisig migrates to a new Wormhole emitter address (key rotation, multisig upgrade, chain migration), the deployed `Executor` contracts on all EVM chains will permanently reject VAAs from the new emitter. All subsequent governance instructions routed through those `Executor` contracts — including contract upgrades, fee changes, and data source updates for downstream Pyth contracts — will be permanently blocked. Recovery requires the proxy owner to perform a direct on-chain upgrade of each `ExecutorUpgradable` instance, bypassing the cross-chain governance mechanism entirely.

---

### Likelihood Explanation

Governance key rotation is a standard operational security practice and is explicitly supported for the Pyth price feed contracts themselves (via `AuthorizeGovernanceDataSourceTransfer`). The absence of an equivalent mechanism for the `Executor` contract means any governance migration will silently break EVM executor functionality. The `TODO` comment in the production source code confirms the developers identified this gap. Likelihood is medium given that governance migrations are planned operational events, not theoretical scenarios.

---

### Recommendation

Add a `ChangeGovernanceDataSource` action to `ExecutorAction` and implement a corresponding handler in `execute()` that updates `ownerEmitterChainId` and `ownerEmitterAddress`. The handler should require the instruction to be signed by the *current* owner emitter (already enforced by `verifyGovernanceVM`) and should include a sequence-number or index guard to prevent replay, analogous to the `governanceDataSourceIndex` used in `PythGovernance.sol`'s `AuthorizeGovernanceDataSourceTransfer`.

---

### Proof of Concept

1. The `ExecutorAction` enum in `Executor.sol` contains only `Execute` with an explicit `TODO` for the missing governance data source change action.
2. `verifyGovernanceVM` hard-checks `ownerEmitterChainId` and `ownerEmitterAddress` with no update path.
3. `_initialize` is the only place these fields are written; it is `internal` and called once at proxy initialization.
4. Submitting any VAA from a new emitter address to `execute()` will always revert with `UnauthorizedEmitter`, regardless of Wormhole guardian signatures. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L34-38)
```text
    // We have different actions here for potential future extensibility
    enum ExecutorAction {
        // TODO: add an instruction to change the governance data source.
        Execute // 0
    }
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L44-61)
```text
    uint16 private ownerEmitterChainId;
    bytes32 private ownerEmitterAddress;

    function _initialize(
        address _wormhole,
        uint64 _lastExecutedSequence,
        uint16 _chainId,
        uint16 _ownerEmitterChainId,
        bytes32 _ownerEmitterAddress
    ) internal {
        require(_wormhole != address(0), "_wormhole is zero address");

        wormhole = IWormhole(_wormhole);
        lastExecutedSequence = _lastExecutedSequence;
        chainId = _chainId;
        ownerEmitterChainId = _ownerEmitterChainId;
        ownerEmitterAddress = _ownerEmitterAddress;
    }
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L66-82)
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

```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L117-137)
```text
    function verifyGovernanceVM(
        bytes memory encodedVM
    ) internal returns (IWormhole.VM memory parsedVM) {
        (IWormhole.VM memory vm, bool valid, ) = wormhole.parseAndVerifyVM(
            encodedVM
        );

        if (!valid) revert ExecutorErrors.InvalidWormholeVaa();

        if (
            vm.emitterChainId != ownerEmitterChainId ||
            vm.emitterAddress != ownerEmitterAddress
        ) revert ExecutorErrors.UnauthorizedEmitter();

        if (vm.sequence <= lastExecutedSequence)
            revert ExecutorErrors.MessageOutOfOrder();

        lastExecutedSequence = vm.sequence;

        return vm;
    }
```
