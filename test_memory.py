"""
Quick smoke-test for backend/memory/database.py.

Run from repo root:
    DB_PATH=/tmp/savant_test.db python3 test_memory.py

Drops and recreates a temp DB so it never touches the real one.
"""

import os
import sys
import tempfile

# Use a throwaway DB for the test
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
os.environ["DB_PATH"] = tmp.name

# Import after setting DB_PATH
from backend.memory.database import (
    init_db,
    is_first_run,
    load_memory,
    save_memory,
    save_session_summary,
    get_recent_sessions,
)

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
errors = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global errors
    if condition:
        print(f"  {PASS}  {label}")
    else:
        print(f"  {FAIL}  {label}" + (f" — {detail}" for _ in [None]).__next__() if detail else f"  {FAIL}  {label}")
        errors += 1


print("\n=== SAVANT memory DB smoke test ===\n")

# --- init_db ----------------------------------------------------------------
print("[1] init_db()")
init_db()
check("no exception on first call", True)
init_db()  # second call must be idempotent
check("idempotent (second call ok)", True)

# --- is_first_run -----------------------------------------------------------
print("\n[2] is_first_run()")
check("True before any owner_name is set", is_first_run() is True)

# --- load_memory ------------------------------------------------------------
print("\n[3] load_memory()")
mem = load_memory()
check("returns non-empty string", bool(mem))
check("contains SAVANT header", "SAVANT" in mem)
print(f"\n--- load_memory() output preview ---\n{mem[:400]}…\n")

# --- save_memory / upsert ---------------------------------------------------
print("[4] save_memory()")
save_memory("owner_name", "TestUser")
check("is_first_run() False after owner_name saved", is_first_run() is False)
mem2 = load_memory()
check("new key appears in load_memory", "owner_name" in mem2)

save_memory("owner_name", "UpdatedUser")
mem3 = load_memory()
check("upsert updates existing key", "UpdatedUser" in mem3)
check("old value gone after upsert", mem3.count("TestUser") == 0)

# --- save_session_summary ---------------------------------------------------
print("\n[5] save_session_summary()")
rid = save_session_summary("Test session: validated memory module end-to-end.")
check("returns integer row id", isinstance(rid, int) and rid > 0)

# --- get_recent_sessions ----------------------------------------------------
print("\n[6] get_recent_sessions()")
sessions = get_recent_sessions(5)
check("returns a list", isinstance(sessions, list))
check("has exactly 1 row (just inserted)", len(sessions) == 1)
check("each row has 'summary' key", all("summary" in s for s in sessions))
check("most recent session is the test one", "Test session" in sessions[0]["summary"])

# --- cleanup ----------------------------------------------------------------
os.unlink(tmp.name)

print(f"\n{'='*38}")
if errors:
    print(f"  {FAIL}  {errors} test(s) FAILED")
    sys.exit(1)
else:
    print(f"  {PASS}  All tests passed")
print()
