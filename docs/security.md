# SAVANT — Security Model

SAVANT has privileged access to your VPS. This document explains every safety mechanism built in to prevent accidental damage.

---

## Access Code Gate

Every WebSocket connection is locked until the owner sends a valid access code.

- The code is stored as a SHA-256 hash in SQLite — the plain text is never persisted
- All audio and commands are dropped until the code is verified
- Invalid codes receive `access_denied` and the connection is closed
- Returning sessions: hash match → immediate unlock
- New sessions: hash saved → onboarding triggered

This prevents unauthorized voice access to the VPS, even if someone finds the WebSocket URL.

---

## Confirmation Gate

No destructive operation executes without explicit voice confirmation.

### For direct commands (`run_command`)

SAVANT **must** ask the owner before executing any of the following:

```
docker stop / restart / kill / rm / prune
systemctl stop / restart / disable
rm / mv / cp  (when overwriting existing files)
truncate
userdel / useradd / passwd
DROP / TRUNCATE / DELETE  (SQL)
iptables -D / -F
any flag: --force / -f / --remove / --delete / --purge
```

**Flow:**
1. SAVANT says: *"I'm going to stop the nginx container. Ready?"*
2. SAVANT stops speaking and waits
3. Owner says: *"oui"* (or "yes", "ok", "go ahead")
4. SAVANT executes

If the owner says "non", "cancel", or "not now" — the action is dropped, never queued.

**Not required for:** `touch`, `mkdir` on new paths, any read-only command.

### For complex Hermes actions (`execute_action`)

Write and critical actions use a two-prong backend gate:

1. First call → SAVANT asks, stores a pending confirmation marker with the current turn count
2. Owner affirms in a **new turn** → `last_affirm_turn` updated
3. Either Gemini re-emits the action with `confirmed=true`, or the backend fires it automatically
4. `confirmed=true` without a new affirm turn → **blocked** — cannot be bypassed

---

## Backup Before Write

Before every write or critical Hermes action, SAVANT automatically:

1. Backs up the relevant file(s) to `/home/savant/backups/hermes/`
2. Stores the backup path in Redis (`savant:last_backup`, 7-day TTL)

**Undo by voice:**
```
"Undo the last change"
"Revert what you just did"
```

This triggers `execute_action(action_type="undo")` which restores from the last backup.

---

## Verification Proof

SAVANT doesn't trust self-reports. After every write/critical action, Hermes must prove the change was applied.

The instruction always includes: `[VERIFY — MANDATORY]`

Hermes must end its response with:
- `VERIFICATION: VERIFIED` — change confirmed
- `VERIFICATION: NOT_VERIFIED — <reason>` — something went wrong

The backend checks the **exact first token** after the colon. Vague language like "it should be done" or "appears to have worked" results in `NOT_VERIFIED`.

Verified actions are logged with `verified=1` in the `action_log` table.

---

## Catastrophic Pattern Blocking

Certain command patterns are **hard-blocked** at the backend level — no confirmation can override them:

```
mkfs.*                    # format filesystem
wipefs                    # wipe filesystem signatures
dd of=/dev/              # direct disk write
rm -rf /etc              # wipe system config
rm -rf / --no-preserve-root
reboot / shutdown / halt / poweroff  # (in dangerous contexts)
:(){ :|:& };:            # fork bomb
kill -9 1 / kill -9 -1  # kill init
```

These patterns trigger an immediate block with a clear error message. The command is never sent to Hermes.

---

## Credential Safety

- **API keys**: stored only in `.env` and `hermes-agent.env` — both gitignored, never committed
- **Workflow auth keys**: stored in SQLite, never returned in `list_workflows()` output, never spoken
- **Access code**: stored as SHA-256 hash only
- **Secrets in logs**: the system prompt instructs Gemini to never speak secrets
- **Redis**: credentials only in `.env`, Redis itself contains no sensitive data

---

## HERMES_WEBHOOK_SECRET

Protects:
- All `/control/*` REST endpoints (pause, stop, resume, status)
- Hermes internal API calls
- The Console Live exec server (`:8644`)

Use a strong random string: `openssl rand -hex 32`

---

## Action Audit Log

Every Hermes action is logged in SQLite `action_log`:
- `action_type` (read/write/critical/undo)
- `description`
- `status` (success/error)
- `duration_s`
- `verified` (0/1)
- `created_at`

Every write/critical action is also appended to `HERMES_CHANGELOG.md`:
```
## 2026-06-04 14:32 UTC — [write] Updated nginx config | Result: ok
```

---

## What SAVANT Cannot Do

By design:
- **No subprocess** from the backend — all VPS access goes through Hermes
- **No unauthenticated control** — `/control/*` requires the shared secret
- **No format/wipe operations** — hard-blocked regardless of confirmation
- **No interactive TTY programs** — vim, top, htop are blocked in Console Live
- **No secret echo** — workflow auth keys and API keys are write-only
