"""Modules with dataclasses must survive being reloaded.

This encodes the failure that took the deployed app down entirely. Streamlit
hot-reloads a changed module by dropping it from sys.modules and
re-importing, and on Python 3.14 that killed the app at import:

    jobs.py:40 in <module>  ->  @dataclass
    dataclasses.py:814 in _is_type
        ns = sys.modules.get(cls.__module__).__dict__
    AttributeError: 'NoneType' object has no attribute '__dict__'

`from __future__ import annotations` makes every field annotation a string,
and dataclasses resolves those through sys.modules with no None check. So a
class body executing while its own module entry is absent cannot construct
its dataclasses.

The test reproduces that condition directly: execute each module's source
in a namespace named after it WITHOUT registering it in sys.modules. Any
module that needs its own sys.modules entry to define its classes fails
here, which is the property we actually want to hold.
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


# Every module that defines a dataclass. Streamlit could reload any of them.
DATACLASS_MODULES = [
    "config", "feed_matching", "feeds", "jobs", "library",
    "pdf_export", "speakers", "summarizer", "transcription",
    "resolvers/common",
]

for rel in DATACLASS_MODULES:
    path = ROOT / (rel + ".py")
    mod_name = rel.replace("/", ".")

    # Import normally first, so dependencies are cached and we isolate the
    # module under test rather than its imports.
    try:
        __import__(mod_name)
    except Exception as e:  # noqa: BLE001
        check(f"{rel} imports normally", False, f"{type(e).__name__}: {e}")
        continue

    saved = sys.modules.pop(mod_name, None)
    module = types.ModuleType(mod_name)
    module.__file__ = str(path)
    module.__package__ = mod_name.rpartition(".")[0]
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
        check(f"{rel} survives reload with no sys.modules entry", True)
    except Exception as e:  # noqa: BLE001
        check(f"{rel} survives reload with no sys.modules entry", False, f"{type(e).__name__}: {e}")
    finally:
        if saved is not None:
            sys.modules[mod_name] = saved

# And the guard against regression: re-adding the future import to any of
# these modules reintroduces the crash on 3.14, so fail if one comes back.
for rel in DATACLASS_MODULES:
    text = (ROOT / (rel + ".py")).read_text(encoding="utf-8")
    offending = [
        line for line in text.splitlines()
        if line.strip().startswith("from __future__ import annotations")
    ]
    check(f"{rel} has no postponed-annotations import", not offending, str(offending))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
