Looking at the RocketPool vulnerability class — **initialization logic that silently skips or fails because a required prerequisite (registration/allowlisting) is not yet established** — I need to find an analog in CKB where initialization logic runs before a required prerequisite is in place, leaving the system in an inconsistent state.

Let me trace the CKB startup path carefully.