"""Smoke-test an INSTALLED `ontary` wheel, with no extras and no dev deps.

Run against a venv that has the wheel and nothing else:

    python scripts/install_smoke.py

Deliberately not a pytest module. It must run where pytest is not installed --
that is the whole point: every other test in this repo runs from the source
tree with `--all-extras`, which is exactly the environment an outside adopter
does NOT have. The first thing a stranger does is `pip install ontary`,
and until this script existed nothing checked that path. It found a real defect
on its first run (`ontary-mcp` raising a bare `ModuleNotFoundError`).

Imports the package by name only, never by path, so it exercises what pip put
in `site-packages` rather than the checkout it happens to be started from.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def check(condition: bool, description: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {description}")
    print(f"  ok: {description}")


def main() -> None:
    print("1. core import (pydantic-only environment)")
    import ontary

    check(bool(ontary.__version__), f"__version__ reports {ontary.__version__!r}")
    check("__version__" in ontary.__all__, "__version__ is exported")
    installed = Path(ontary.__file__).resolve().parent
    check(
        "site-packages" in installed.parts,
        f"imported from an install, not a checkout ({installed})",
    )
    check((installed / "py.typed").is_file(), "py.typed ships inside the package")

    from importlib.metadata import distribution, metadata

    check(
        metadata("ontary")["License-Expression"] == "MIT",
        "installed metadata declares the MIT license expression",
    )
    # An SBOM scanner and a corporate license audit both read the shipped file,
    # not the source tree's LICENSE -- so check the artifact carries it.
    license_files = [
        f
        for f in distribution("ontary").files or []
        if "licenses/LICENSE" in str(f)
    ]
    check(bool(license_files), "the LICENSE text ships inside the distribution")

    print("2. a governed runtime comes up")
    # 0.6.0 moved the runtime entry points one namespace deeper and replaced
    # the legacy exception classes with the kind hierarchy. These are the
    # import paths docs/storage.md and docs/effects.md teach, so the smoke
    # test exercises what a reader is actually told to write.
    from ontary import (
        Consumer,
        DirectProperty,
        Ontology,
        OntologyObject,
        SelfScope,
        Source,
        VisibilityError,
        prop,
    )
    from ontary.client import OntologyClient
    from ontary.store import InMemoryStore

    ontology = Ontology("smoke", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Org(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0", scope=[DirectProperty(level="org", property_name="org_id")]
    )
    class Doc(OntologyObject):
        id: str = prop(primary_key=True)
        org_id: str

    ontology.validate()
    store = InMemoryStore(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="smoke",
            role="Reader",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
    )
    store.insert("Doc", {"id": "d-1", "org_id": "org-1"}, Source(source_system="smoke"))
    check(client.get("Doc", "d-1") is not None, "in-scope read succeeds")

    print("3. the guard actually guards")
    store.insert("Doc", {"id": "d-2", "org_id": "org-2"}, Source(source_system="smoke"))
    try:
        client.get("Doc", "d-2")
    except VisibilityError as exc:
        check(
            exc.code == "VISIBILITY_DENIED",
            f"out-of-scope read is refused with a stable code ({exc.code})",
        )
    else:
        raise SystemExit("FAIL: out-of-scope read was NOT refused")

    print("4. connectors import without the dlt extra")
    import ontary.connect  # noqa: F401

    check(True, "ontary.connect imports")

    print("5. the MCP entrypoint explains its missing extra")
    result = subprocess.run(
        [sys.executable, "-m", "ontary.mcp_server"],
        capture_output=True,
        text=True,
    )
    message = result.stdout + result.stderr
    check(result.returncode != 0, "ontary-mcp exits non-zero without the extra")
    check(
        "ontary[mcp]" in message,
        "the failure names the extra to install, not a raw ImportError",
    )
    check(
        "Traceback" not in message,
        "no traceback -- a first-contact message, not a crash",
    )

    print("6. the CLI entrypoint is installed")
    ontary_command = Path(sys.executable).with_name("ontary")
    check(
        ontary_command.exists(),
        f"ontary console script exists next to the interpreter ({ontary_command})",
    )
    version_result = subprocess.run(
        [str(ontary_command), "version"],
        capture_output=True,
        text=True,
    )
    check(version_result.returncode == 0, "ontary version exits successfully")
    check(
        version_result.stdout.strip() == ontary.__version__,
        "ontary version reports the installed package version",
    )

    print("\nPASS: the installed package is usable.")


if __name__ == "__main__":
    main()
