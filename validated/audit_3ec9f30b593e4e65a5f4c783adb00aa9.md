### Title
`PRNG.sol` `private seed` and `nonce` Are Publicly Readable On-Chain, Allowing Full Prediction of All PRNG Outputs - (File: `target_chains/ethereum/entropy_sdk/solidity/PRNG.sol`)

---

### Summary

The `PRNG` contract in the Pyth Entropy SDK stores its internal state in two `private` state variables — `bytes32 private seed` and `uint256 private nonce`. The `private` visibility modifier only prevents other Solidity contracts from reading these variables; it does not prevent any blockchain observer from reading them via `eth_getStorageAt`. Any attacker who reads these two storage slots can deterministically predict every future output of the PRNG, completely breaking the randomness guarantee for any contract that inherits from `PRNG`.

---

### Finding Description

`PRNG.sol` declares:

```solidity
bytes32 private seed;
uint256 private nonce;
``` [1](#0-0) 

Every output is computed as:

```solidity
bytes32 result = keccak256(abi.encode(seed, nonce));
nonce++;
``` [2](#0-1) 

Because `seed` occupies storage slot 0 and `nonce` occupies storage slot 1 of any inheriting contract, any external observer can call:

```
eth_getStorageAt(contractAddress, 0)  // → seed
eth_getStorageAt(contractAddress, 1)  // → nonce
```

With both values known, the attacker can reproduce the exact sequence `keccak256(abi.encode(seed, nonce))`, `keccak256(abi.encode(seed, nonce+1))`, … for all future calls, making every output of `nextBytes32()`, `randUint()`, `randUint64()`, `randUintRange()`, and `randomPermutation()` fully predictable.

The SDK README explicitly promotes this pattern for production use:

```solidity
contract MyContract is PRNG {
  constructor(bytes32 _seed) {
    PRNG(_seed);
  }
}
``` [3](#0-2) 

The seed itself is set from a Pyth Entropy callback value, which is a legitimate source of entropy at the moment of seeding. However, once written to storage, it is permanently and trivially readable by anyone.

---

### Impact Explanation

Any deployed contract that inherits from `PRNG` and uses its functions for randomness (NFT mints, on-chain games, lotteries, permutation-based selection) has all its "random" outputs fully predictable by any blockchain observer. An attacker can:

1. Read `seed` and `nonce` from storage before a target transaction.
2. Compute the exact sequence of future outputs off-chain.
3. Exploit the known outcome (e.g., front-run an NFT mint to claim the winning token, predict a game result, or manipulate a permutation-based selection).

This completely defeats the purpose of using Pyth Entropy as a randomness source, since the entropy is immediately exposed in contract storage.

---

### Likelihood Explanation

Exploitation requires no special privileges, no gas beyond a standard RPC call, and no cooperation from any trusted party. `eth_getStorageAt` is a standard JSON-RPC method available on every EVM-compatible chain. Any user of the blockchain can perform this read at any time. Likelihood is high.

---

### Recommendation

Do not store the PRNG seed and nonce as plain `private` state variables. Options include:

1. **Transient storage (EIP-1153):** Use `tstore`/`tload` so the seed is not persisted across transactions.
2. **Re-seed on every use:** Request a fresh Entropy value for each randomness need rather than maintaining a stateful PRNG.
3. **Document the limitation clearly:** If the contract is intended only for use cases where predictability is acceptable (e.g., non-adversarial contexts), add an explicit warning that the seed is publicly readable and the PRNG is not suitable for adversarial randomness.

---

### Proof of Concept

Given a deployed `MyContract is PRNG` at address `TARGET`:

```solidity
// Attacker reads storage off-chain (standard RPC):
bytes32 seed  = vm.load(TARGET, bytes32(uint256(0)));
uint256 nonce = uint256(vm.load(TARGET, bytes32(uint256(1))));

// Attacker predicts the next N outputs:
for (uint i = 0; i < N; i++) {
    bytes32 predicted = keccak256(abi.encode(seed, nonce + i));
    console.log(predicted); // matches MyContract's next randUint() outputs exactly
}
```

This mirrors the PasswordStore exploit: read storage slot → recover the "private" value → exploit full knowledge of future state. [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/entropy_sdk/solidity/PRNG.sol (L9-33)
```text
contract PRNG {
    bytes32 private seed;
    uint256 private nonce;

    /// @notice Initialize the PRNG with a seed
    /// @param _seed The Pyth Entropy seed (bytes32)
    constructor(bytes32 _seed) {
        seed = _seed;
        nonce = 0;
    }

    /// @notice Set a new seed and reset the nonce
    /// @param _newSeed The new seed (bytes32)
    function setSeed(bytes32 _newSeed) internal {
        seed = _newSeed;
        nonce = 0;
    }

    /// @notice Generate the next random bytes32 value and update the state
    /// @return The next random bytes32 value
    function nextBytes32() internal returns (bytes32) {
        bytes32 result = keccak256(abi.encode(seed, nonce));
        nonce++;
        return result;
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/PRNG.sol (L63-76)
```text
    /// @return A randomly permuted array of uint256 values
    function randomPermutation(
        uint256 length
    ) internal returns (uint256[] memory) {
        uint256[] memory permutation = new uint256[](length);
        for (uint256 i = 0; i < length; i++) {
            permutation[i] = i;
        }
        for (uint256 i = 0; i < length; i++) {
            uint256 j = i + (randUint() % (length - i));
            (permutation[i], permutation[j]) = (permutation[j], permutation[i]);
        }
        return permutation;
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/README.md (L126-136)
```markdown
To use the PRNG contract in your project:

1. Create a contract that inherits from PRNG and uses its internal functions with a seed from Pyth Entropy:

```solidity
contract MyContract is PRNG {
  constructor(bytes32 _seed) {
    PRNG(_seed);
  }
}
```
```
