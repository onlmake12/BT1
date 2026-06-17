### Title
Incorrect `DUMMY_CHAIN_ID = 1` in Fuel Deployment Script Enables Cross-Chain Governance Replay — (File: `target_chains/fuel/contracts/src/constants.rs`)

---

### Summary

The Fuel Pyth contract deployment script initializes the on-chain `chain_id` storage slot with `DUMMY_CHAIN_ID = 1` — Solana's Wormhole chain ID — instead of the correct Fuel testnet chain ID (`50084`). Because the governance instruction validator checks `gi.target_chain_id == chain_id()`, any governance VAA legitimately issued for Solana (target chain 1) passes the chain-ID gate on the Fuel contract and is executed there. This is a direct analog to the Opera-Bridge bug: a wrong chain ID in a deployment configuration causes the contract to accept messages intended for a different chain.

---

### Finding Description

**Root cause — wrong constant in deployment source:**

`target_chains/fuel/contracts/src/constants.rs` line 24 defines:

```rust
pub const DUMMY_CHAIN_ID: u16 = 1;
```

`1` is Solana's Wormhole chain ID. The correct Pyth chain ID for Fuel testnet is `50084` and for Fuel mainnet is `60067`, as registered in `governance/xc_admin/packages/xc_admin_common/src/chains.ts` lines 100 and 224.

**Deployment script passes this constant directly to the constructor:**

`target_chains/fuel/contracts/scripts/deploy_pyth.rs` line 54:

```rust
DUMMY_CHAIN_ID,   // chain_id argument
```

This writes `1` into `storage.chain_id` at deployment time (`main.sw` line 543: `storage.chain_id.write(chain_id)`).

**Governance validation uses the stored chain_id:**

`target_chains/fuel/contracts/pyth-contract/src/main.sw` lines 742–745:

```sway
require(
    gi.target_chain_id == chain_id() || gi.target_chain_id == 0,
    PythError::InvalidGovernanceTarget,
);
```

`chain_id()` reads `storage.chain_id` (line 467–469). With `chain_id = 1`, any governance instruction carrying `target_chain_id = 1` (Solana) passes this check.

**Governance data source is also chain 1:**

The deployment script sets the governance data source to `chain_id: 1` with the Pyth governance emitter address `5635979a...` — the same emitter used for all Pyth governance VAAs on Solana. So the emitter-origin check also passes for Solana-targeted VAAs.

**Sequence tracking is per-contract, not global:**

The Fuel contract starts with `last_executed_governance_sequence = 0` and advances it independently. Every governance VAA issued for Solana (sequence 1, 2, 3 …) can be submitted to the Fuel contract in order; none are blocked by the sequence check.

**The `chains.ts` comment confirms the mainnet contract also carries the wrong chain ID:**

Line 100: `fuel_mainnet: 60067, // Note: Currently deployed at 50084 (fuel_testnet) but we should use 60067 for future deployments` — the live mainnet contract was initialized with `50084` (the testnet chain ID), so governance VAAs targeting Fuel testnet (`50084`) are accepted on Fuel mainnet.

---

### Impact Explanation

An unprivileged attacker who observes any Pyth governance VAA issued for Solana (target_chain_id = 1, emitter = `5635979a…`) can submit it to the Fuel testnet contract. The contract will:

1. Accept the VAA (emitter check passes — same governance emitter; chain-ID check passes — `1 == 1`).
2. Execute the embedded governance action: `SetFee`, `SetDataSources`, `SetValidPeriod`, or `AuthorizeGovernanceDataSourceTransfer`.

Concrete consequences:
- **Fee manipulation**: attacker replays a Solana `SetFee` VAA to set an arbitrarily high or zero fee on Fuel, breaking price-update economics.
- **Data-source hijack**: replaying a Solana `SetDataSources` VAA replaces the valid Fuel data sources with Solana-specific emitters, causing all Fuel price updates to be rejected as `InvalidDataSource`.
- **Valid-period change**: replaying a `SetValidPeriod` VAA makes all Fuel prices appear stale or never stale.

For the mainnet contract (chain_id = 50084), the same attack applies using governance VAAs issued for Fuel testnet.

---

### Likelihood Explanation

- Governance VAAs are broadcast publicly on Wormhole and are trivially observable.
- No key, signature, or privileged access is required — the attacker only needs to relay an existing, guardian-signed VAA.
- Pyth governance actions for Solana are issued regularly; the attack window is continuous.
- The sequence-number check does not block the attack; it only prevents replaying the *same* VAA twice.

---

### Recommendation

1. Replace `DUMMY_CHAIN_ID = 1` in `target_chains/fuel/contracts/src/constants.rs` with the correct Pyth chain IDs: `50084` for testnet and `60067` for mainnet.
2. Redeploy (or upgrade via governance) the Fuel contracts with the correct `chain_id` values.
3. Add a deployment-time assertion that `chain_id != 0 && chain_id != 1` to prevent accidental use of placeholder values.
4. Align the `chains.ts` comment for `fuel_mainnet` with the actual on-chain value and track the discrepancy as a known misconfiguration until redeployment.

---

### Proof of Concept

```
1. Observe any Pyth governance VAA on Wormhole with:
      emitter_chain  = 1  (Solana)
      emitter_addr   = 5635979a221c34931e32620b9293a463065555ea71fe97cd6237ade875b12e9e
      payload[6:8]   = 0x0001  (target_chain_id = 1)
      e.g. a SetFee instruction

2. Call execute_governance_instruction(vaa_bytes) on the Fuel testnet
   Pyth contract (0x5d17f54708afd01530c2e0ffb123cd21e92461aae8450de2cc08d0fd330cf240).

3. Validation path in main.sw:
      - governance_data_source check: chain_id=1, emitter=5635... ✓ (matches stored source)
      - sequence check: vm.sequence > last_executed_governance_sequence() ✓ (first replay)
      - target_chain_id check: 1 == chain_id() (== 1) ✓

4. Governance action executes on Fuel; e.g. single_update_fee is changed to the
   value encoded in the Solana VAA.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/fuel/contracts/src/constants.rs (L24-24)
```rust
pub const DUMMY_CHAIN_ID: u16 = 1;
```

**File:** target_chains/fuel/contracts/scripts/deploy_pyth.rs (L47-55)
```rust
    let _ = pyth
        .constructor(
            governance_data_source,
            wormhole_governance_data_source,
            DEFAULT_VALID_TIME_PERIOD,
            guardian_set_upgrade_4_addresses(),
            4,
            DUMMY_CHAIN_ID,
        )
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L466-469)
```text
#[storage(read)]
fn chain_id() -> u16 {
    storage.chain_id.read()
}
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L543-543)
```text
        storage.chain_id.write(chain_id);
```

**File:** target_chains/fuel/contracts/pyth-contract/src/main.sw (L742-745)
```text
    require(
        gi.target_chain_id == chain_id() || gi.target_chain_id == 0,
        PythError::InvalidGovernanceTarget,
    );
```

**File:** governance/xc_admin/packages/xc_admin_common/src/chains.ts (L100-100)
```typescript
  fuel_mainnet: 60067, // Note: Currently deployed at 50084 (fuel_testnet) but we should use 60067 for future deployments
```

**File:** governance/xc_admin/packages/xc_admin_common/src/chains.ts (L224-224)
```typescript
  fuel_testnet: 50084,
```
