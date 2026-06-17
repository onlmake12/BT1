### Title
Unprotected `advance_clock` Instruction Allows Arbitrary Time Manipulation in Staking Program — (`governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The Pyth staking program exposes an `advance_clock` instruction that requires **no signer and no authority check**. It directly writes an attacker-controlled `seconds: i64` value into `GlobalConfig.mock_clock_time`, which the program uses as the authoritative clock for epoch computation, vesting schedules, and staking position cooldowns. This is a direct structural analog to the DSWarp vulnerability: a time-manipulation function intended for testing that was left in the production program without adequate access control.

---

### Finding Description

The `advance_clock` instruction is defined in the deployed staking program IDL with a single account — the writable `config` PDA — and no signer constraint:

```json
{
  "accounts": [
    {
      "name": "config",
      "pda": { "seeds": [{ "kind": "const", "value": [99,111,110,102,105,103] }] },
      "writable": true
    }
  ],
  "args": [{ "name": "seconds", "type": "i64" }],
  "discriminator": [52, 57, 147, 111, 56, 227, 33, 127],
  "name": "advance_clock"
}
``` [1](#0-0) 

The `GlobalConfig` struct stores the result in `mock_clock_time: i64`: [2](#0-1) 

The same field appears in the integrity-pool IDL, confirming it is shared across both the staking and reward programs: [3](#0-2) 

The TypeScript IDL type definition confirms the instruction signature — no signer, no authority relation: [4](#0-3) 

The older multisig admin IDL also exposes the same instruction with identical constraints: [5](#0-4) 

The program does define a `debuggingOnly` error (code 6018, "Not allowed when not debugging"): [6](#0-5) 

However, the Rust source for the staking program is not indexed in this repository snapshot, so the exact runtime condition that activates "debugging mode" cannot be confirmed. The critical concern is:

1. The instruction is compiled into the production binary (it appears in the deployed IDL).
2. No signer or authority account is required — the only account is the config PDA itself.
3. If `mock_clock_time != 0` is the condition that enables the instruction (i.e., the mock clock is already active), any user who can observe the on-chain state can call `advance_clock` freely once the mock clock is initialized.
4. Even if a separate `is_debug` flag exists, the design places a test-only time-warp primitive in the production program with no cryptographic access control.

---

### Impact Explanation

`mock_clock_time` is used as the authoritative timestamp for:

- **Epoch computation**: staking epochs determine voting weight snapshots and reward distribution windows.
- **Vesting schedules**: `pyth_token_list_time` and epoch-based vesting depend on the current clock.
- **Cooldown enforcement**: unstaking cooldown periods are measured against the current epoch/time.
- **Integrity pool reward accrual**: `last_updated_epoch` in `PoolData` is compared against the current epoch derived from the clock.

An attacker who can call `advance_clock` with an arbitrary `seconds` value can:
- Skip epochs to prematurely unlock staked tokens (bypassing cooldown).
- Accelerate vesting to claim tokens before the intended schedule.
- Manipulate reward accrual windows to claim disproportionate rewards.
- Corrupt the `last_updated_epoch` accounting in the integrity pool.

---

### Likelihood Explanation

**Medium-to-High** if the mock clock is active on-chain (i.e., `mock_clock_time != 0`). The instruction requires no privileged key — any funded Solana wallet can submit the transaction. The only barrier is whether the `debuggingOnly` guard is enforced and what condition activates it. Given the instruction is present in the production IDL with no signer constraint, the risk is real if the program was ever initialized with `mock_clock_time != 0` or if the debug flag can be set through another path.

---

### Recommendation

1. **Remove `advance_clock` from the production binary entirely.** If it is needed for testing, gate it behind `#[cfg(test)]` or a separate test-only program, not a feature flag that can be compiled into the production deployment.
2. **If the instruction must remain**, add a `governance_authority` signer constraint (matching the pattern used by all other privileged instructions in the same program) so only the governance key can call it.
3. **Audit `mock_clock_time` initialization**: ensure it is always set to `0` in production deployments and that no instruction other than a governance-signed one can set it to a non-zero value.
4. **Document the deployment invariant** explicitly: `mock_clock_time` must be `0` in production, and `advance_clock` must never be callable on mainnet.

---

### Proof of Concept

```
# Attacker submits a transaction to the staking program on Solana mainnet
# Instruction discriminator: [52, 57, 147, 111, 56, 227, 33, 127]
# Accounts: [config PDA (writable)]
# Args: seconds = 31_536_000  (advance clock by 1 year)
#
# If mock_clock_time is active (non-zero), this succeeds with no privileged key.
# Effect: all epoch-dependent logic (vesting, cooldowns, rewards) now
# evaluates as if 1 year has elapsed, allowing the attacker to:
#   - Immediately close staking positions that were in cooldown
#   - Claim vested tokens ahead of schedule
#   - Drain integrity pool rewards for epochs that haven't occurred
```

The instruction requires only the config PDA (a deterministic address, publicly derivable) and an arbitrary `seconds` argument — no signature from any authority is needed.

### Citations

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L425-448)
```json
    {
      "accounts": [
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          },
          "writable": true
        }
      ],
      "args": [
        {
          "name": "seconds",
          "type": "i64"
        }
      ],
      "discriminator": [52, 57, 147, 111, 56, 227, 33, 127],
      "name": "advance_clock"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1873-1876)
```json
          {
            "name": "mock_clock_time",
            "type": "i64"
          },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1324-1327)
```json
          {
            "name": "mock_clock_time",
            "type": "i64"
          },
```

**File:** governance/pyth_staking_sdk/src/types/staking.ts (L232-255)
```typescript
    {
      name: "advanceClock";
      discriminator: [52, 57, 147, 111, 56, 227, 33, 127];
      accounts: [
        {
          name: "config";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
      ];
      args: [
        {
          name: "seconds";
          type: "i64";
        },
      ];
    },
```

**File:** governance/pyth_staking_sdk/src/types/staking.ts (L1932-1935)
```typescript
      code: 6018;
      name: "debuggingOnly";
      msg: "Not allowed when not debugging";
    },
```

**File:** governance/xc_admin/packages/xc_admin_common/src/multisig_transaction/idl/staking.json (L1339-1363)
```json
    {
      "accounts": [
        {
          "isMut": true,
          "isSigner": false,
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "type": "string",
                "value": "config"
              }
            ]
          }
        }
      ],
      "args": [
        {
          "name": "seconds",
          "type": "i64"
        }
      ],
      "name": "advanceClock"
    },
```
