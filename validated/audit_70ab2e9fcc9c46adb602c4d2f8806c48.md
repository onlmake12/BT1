### Title
Unprotected `setup()` Allows Anyone to Initialize WormholeReceiver/Wormhole with Malicious Guardian Set Before Deployer — (`File: target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverSetup.sol`, `target_chains/ethereum/contracts/contracts/wormhole/Setup.sol`)

---

### Summary

Both `ReceiverSetup.setup()` and `Setup.setup()` are `public` with **no caller restriction and no "already initialized" guard**. When the proxy (`WormholeReceiver` / `Wormhole`) is deployed with empty `initData` — a pattern explicitly present in the codebase — the proxy's implementation remains `ReceiverSetup` until `setup()` is called in a separate transaction. Any unprivileged attacker monitoring the mempool can front-run that call and inject attacker-controlled guardian addresses, a malicious governance contract, and an arbitrary implementation address.

---

### Finding Description

`ReceiverSetup.setup()` is declared `public` with no `onlyDeployer`, `onlyOwner`, or `initializer` modifier: [1](#0-0) 

The identical pattern exists in the Wormhole `Setup` contract: [2](#0-1) 

The proxy constructor accepts empty `initData`:

```solidity
constructor(address setup, bytes memory initData) ERC1967Proxy(setup, initData) {}
``` [3](#0-2) 

When `initData` is empty, `ERC1967Proxy` sets the implementation to `ReceiverSetup` but **does not call `setup()`**. The proxy sits uninitialized on-chain. The test utility confirms this two-step pattern is a supported deployment path:

```solidity
Wormhole wormhole = new Wormhole(address(wormholeSetup), new bytes(0));
// ... separate tx:
Setup(address(wormhole)).setup(address(wormholeImpl), initSigners, ...);
``` [4](#0-3) 

During the window between proxy deployment and the deployer's `setup()` call, any address can call `setup()` on the proxy with:
- Attacker-controlled `initialGuardians`
- Attacker-controlled `governanceContract` / `governanceChainId`
- An arbitrary `implementation` address

After the attacker's `setup()` executes, `_upgradeTo(implementation)` replaces the implementation with the attacker's address. The deployer's subsequent `setup()` call then executes against the attacker's implementation.

`setup()` contains **no re-entrancy guard and no initialized flag**: [5](#0-4) 

Compare with `ReceiverImplementation`, which does have an `initializer` modifier that checks `isInitialized`: [6](#0-5) 

The `setup()` function has no equivalent protection.

---

### Impact Explanation

The `WormholeReceiver` / `Wormhole` proxy is the trust root for VAA verification in Pyth's EVM price feed pipeline. Guardian addresses stored by `setup()` are the sole authority used to verify every VAA signature: [7](#0-6) 

If an attacker controls the guardian set at initialization:
1. They can produce VAAs signed by their own keys that pass `verifyVM`.
2. These VAAs are accepted by `PythUpgradable` as legitimate price attestations.
3. The attacker can publish arbitrary price data for any price feed, enabling unlimited manipulation of any protocol consuming Pyth prices on that chain.

---

### Likelihood Explanation

**Current production deployment scripts** (`batchDeployReceivers.ts`, `zkSyncDeployWormhole.ts`, `Deploy.s.sol`) all pass encoded `initData` to the proxy constructor, making initialization atomic and closing the window. [8](#0-7) 

However:
- The contract itself enforces **nothing** — no guard prevents the two-step pattern.
- The test utility explicitly demonstrates the two-step pattern as a valid code path.
- Any new chain deployment that deviates from the script (e.g., manual deployment, tooling error, or a new deployment framework) would be immediately exploitable by a mempool-watching bot.
- Likelihood is **medium** for new deployments; **low** for already-deployed instances (already initialized atomically).

---

### Recommendation

Add an "already initialized" guard to `setup()` in both `ReceiverSetup` and `Setup`, analogous to the `initializer` modifier already present in `ReceiverImplementation`:

```solidity
function setup(...) public {
    require(!isInitialized(address(this)), "already initialized");
    setInitialized(address(this));
    // ... rest of setup
}
```

Alternatively, restrict `setup()` to `tx.origin == deployer` (with the same caveats the RocketPool report noted), or enforce atomic initialization by reverting in the constructor if `initData` is empty.

---

### Proof of Concept

```solidity
// 1. Attacker watches mempool for WormholeReceiver deployment with empty initData
// 2. Proxy deployed: new WormholeReceiver(receiverSetupAddr, "")
//    → implementation = ReceiverSetup, setup() not yet called

// 3. Attacker front-runs deployer's setup() call:
address[] memory fakeGuardians = new address[](1);
fakeGuardians[0] = attackerAddress;
ReceiverSetup(proxyAddress).setup(
    maliciousImplementation,  // attacker-controlled impl
    fakeGuardians,            // attacker controls guardian set
    chainId,
    governanceChainId,
    governanceContract
);
// → proxy now has attacker as sole guardian, impl = maliciousImplementation

// 4. Attacker signs arbitrary VAA with their private key
// 5. VAA passes verifyVM() in ReceiverGovernance / Messages
// 6. Pyth accepts attacker-signed price attestation
// 7. Price feeds manipulated at will
``` [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverSetup.sol (L12-33)
```text
    function setup(
        address implementation,
        address[] memory initialGuardians,
        uint16 chainId,
        uint16 governanceChainId,
        bytes32 governanceContract
    ) public {
        require(initialGuardians.length > 0, "no guardians specified");

        ReceiverStructs.GuardianSet memory initialGuardianSet = ReceiverStructs
            .GuardianSet({keys: initialGuardians, expirationTime: 0});

        storeGuardianSet(initialGuardianSet, 0);
        // initial guardian set index is 0, which is the default value of the storage slot anyways

        setChainId(chainId);

        setGovernanceChainId(governanceChainId);
        setGovernanceContract(governanceContract);

        _upgradeTo(implementation);
    }
```

**File:** target_chains/ethereum/contracts/contracts/wormhole/Setup.sol (L12-35)
```text
    function setup(
        address implementation,
        address[] memory initialGuardians,
        uint16 chainId,
        uint16 governanceChainId,
        bytes32 governanceContract
    ) public {
        require(initialGuardians.length > 0, "no guardians specified");

        Structs.GuardianSet memory initialGuardianSet = Structs.GuardianSet({
            keys: initialGuardians,
            expirationTime: 0
        });

        storeGuardianSet(initialGuardianSet, 0);
        // initial guardian set index is 0, which is the default value of the storage slot anyways

        setChainId(chainId);

        setGovernanceChainId(governanceChainId);
        setGovernanceContract(governanceContract);

        _upgradeTo(implementation);
    }
```

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/WormholeReceiver.sol (L8-12)
```text
contract WormholeReceiver is ERC1967Proxy {
    constructor(
        address setup,
        bytes memory initData
    ) ERC1967Proxy(setup, initData) {}
```

**File:** target_chains/ethereum/contracts/test/utils/WormholeTestUtils.t.sol (L30-48)
```text
        Wormhole wormhole = new Wormhole(address(wormholeSetup), new bytes(0));

        address[] memory initSigners = new address[](numGuardians);
        currentSigners = new uint256[](numGuardians);

        for (uint256 i = 0; i < numGuardians; ++i) {
            currentSigners[i] = i + 1;
            initSigners[i] = vm.addr(currentSigners[i]); // i+1 is the private key for the i-th signer.
        }

        // These values are the default values used in our tilt test environment
        // and are not important.
        Setup(address(wormhole)).setup(
            address(wormholeImpl),
            initSigners,
            CHAIN_ID, // Ethereum chain ID
            GOVERNANCE_CHAIN_ID, // Governance source chain ID (1 = solana)
            GOVERNANCE_CONTRACT // Governance source address
        );
```

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverImplementation.sol (L12-20)
```text
    modifier initializer() {
        address implementation = ERC1967Upgrade._getImplementation();

        require(!isInitialized(implementation), "already initialized");

        setInitialized(implementation);

        _;
    }
```

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverGovernance.sol (L59-87)
```text
    function verifyGovernanceVM(
        ReceiverStructs.VM memory vm
    ) internal view returns (bool, string memory) {
        // validate vm
        (bool isValid, string memory reason) = verifyVM(vm);
        if (!isValid) {
            return (false, reason);
        }

        // only current guardianset can sign governance packets
        if (vm.guardianSetIndex != getCurrentGuardianSetIndex()) {
            return (false, "not signed by current guardian set");
        }

        // verify source
        if (uint16(vm.emitterChainId) != governanceChainId()) {
            return (false, "wrong governance chain");
        }
        if (vm.emitterAddress != governanceContract()) {
            return (false, "wrong governance contract");
        }

        // prevent re-entry
        if (governanceActionIsConsumed(vm.hash)) {
            return (false, "governance action already consumed");
        }

        return (true, "");
    }
```

**File:** contract_manager/scripts/common.ts (L343-358)
```typescript
  const initData = setupContract.methods
    .setup(
      receiverImplAddr,
      wormholeConfig.initialGuardianSet.map((addr: string) => "0x" + addr),
      chain.getWormholeChainId(),
      wormholeConfig.governanceChainId,
      "0x" + wormholeConfig.governanceContract,
    )
    .encodeABI();

  const wormholeReceiverAddr = await deployIfNotCached(
    cacheFile,
    chain,
    config,
    "WormholeReceiver",
    [receiverSetupAddr, initData],
```
