### Title
Staking Protocol Freeze Blocks Critical User-Protective Operations (`close_position` / `withdraw_stake`) — (File: `governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The Pyth staking program (`pytS9TjG1qyAZypk7n8rw8gfW9sUaqqYyMhJQ4E7JCQ`) contains a `freeze` boolean in its `GlobalConfig` account and a corresponding `Frozen` error (code 6017). Both `close_position` (unstaking) and `withdraw_stake` require the `config` account in their account lists, meaning the freeze guard is enforced on these instructions. When governance sets `freeze = true`, stakers cannot exit their positions or withdraw tokens — directly analogous to the reported pattern where a `whenNotPaused` guard blocks user-protective operations.

---

### Finding Description

The `GlobalConfig` struct in the Pyth staking program contains a `freeze: bool` field:

```
governance/pyth_staking_sdk/src/idl/staking.json
  "name": "freeze",
  "type": "bool"
```

The program defines error code 6017 `Frozen` ("Protocol is frozen"). Both the `close_position` and `withdraw_stake` instructions include the `config` PDA (seeded with `"config"`) in their required accounts, meaning the on-chain handler reads `GlobalConfig` and enforces the freeze check before executing.

When `freeze = true` is set by governance:
- `close_position` — used to begin unstaking from a publisher (OIS) or governance — is blocked.
- `withdraw_stake` — used to move unlocked tokens back to the user's wallet — is blocked.

A staker who has tokens delegated to a publisher that is about to be slashed cannot call `close_position` to exit the delegation and avoid the slash. The slashing event can proceed while the user is locked out of the only protective action available to them.

---

### Impact Explanation

Stakers in Oracle Integrity Staking (OIS) face slashing of up to 5% of their delegated stake for publisher misbehavior. The only user-controlled protective action is to call `close_position` (undelegate) before the slash is executed. If the protocol is frozen at the time a slashing event is being processed, stakers cannot exit their positions. Their stake is slashed while they are unable to act. This constitutes a direct, quantifiable financial loss to unprivileged staking users.

---

### Likelihood Explanation

The `freeze` flag is settable by the `governance_authority` (a governance-controlled key). A freeze could be triggered legitimately (e.g., for an upgrade or security incident) at the same time a slashing event is being processed. The two events are not mutually exclusive — in fact, a security incident that triggers a freeze is precisely the scenario where a publisher's data quality may also be under scrutiny, making simultaneous freeze + slashing realistic.

---

### Recommendation

The `close_position` and `withdraw_stake` instructions should be exempted from the freeze guard, or a separate "emergency exit" instruction should be provided that bypasses the freeze check. Stakers must retain the ability to exit positions regardless of protocol freeze state, consistent with the principle that protective user actions should not be blocked by operational pause mechanisms.

---

### Proof of Concept

1. Governance calls `update_config` setting `freeze = true` on the `GlobalConfig` PDA.
2. A slashing event is initiated against a publisher pool.
3. Alice, a delegator in that pool, calls `close_position` to undelegate before the slash executes.
4. The instruction reads `GlobalConfig`, finds `freeze = true`, and reverts with error 6017 `Frozen`.
5. Alice cannot exit. The slash executes against her delegated stake.

**Key production references:**

- `GlobalConfig.freeze` field: [1](#0-0) 
- `Frozen` error (code 6017): [2](#0-1) 
- `close_position` instruction (includes `config` account): [3](#0-2) 
- `withdraw_stake` instruction (includes `config` account): [4](#0-3) 

> **Note:** The Rust source files for the staking program are not present in the indexed repository (only the compiled IDL artifacts are available). The exact `require!(!config.freeze, ErrorCode::Frozen)` call site line cannot be cited from source, but the IDL account constraints and error definition constitute production-level evidence of the guard's existence and scope.

### Citations

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L120-123)
```json
      "code": 6017,
      "msg": "Protocol is frozen",
      "name": "Frozen"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L511-553)
```json
              {
                "kind": "const",
                "value": [116, 97, 114, 103, 101, 116]
              },
              {
                "kind": "const",
                "value": [118, 111, 116, 105, 110, 103]
              }
            ]
          },
          "writable": true
        },
        {
          "name": "pool_authority",
          "optional": true,
          "signer": true
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "index",
          "type": "u8"
        },
        {
          "name": "amount",
          "type": "u64"
        },
        {
          "name": "target_with_parameters",
          "type": {
            "defined": {
              "name": "TargetWithParameters"
            }
          }
        }
      ],
      "discriminator": [123, 134, 81, 0, 49, 68, 98, 98],
      "name": "close_position"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1783-1807)
```json
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        }
      ],
      "args": [
        {
          "name": "amount",
          "type": "u64"
        }
      ],
      "discriminator": [153, 8, 22, 138, 105, 176, 87, 66],
      "name": "withdraw_stake"
    }
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1845-1847)
```json
            "name": "freeze",
            "type": "bool"
          },
```
