"""The lock file and the direct requirements must not drift apart.

requirements.txt names the three direct dependencies; requirements.lock
names the whole set the image installs. Before the lock existed, the
three pins were exact and the other thirteen were resolved at build
time -- a rebuild a month later could install different versions of
them.

The tripwire is on the property (the lock contains the direct pins,
every entry is pinned, and the image installs the lock), not on the
number of packages: the number changes whenever a dependency changes.
"""
import pathlib
import re

BOT = pathlib.Path(__file__).resolve().parent.parent / "telegram-bot"
ROOT = BOT.parent

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)\s*(#.*)?$")


def _pins(path: pathlib.Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = PIN.match(stripped)
        assert match, f"{path.name}: {stripped!r} is not an exact pin"
        out[match.group(1).lower()] = match.group(2)
    return out


def test_the_lock_pins_every_direct_dependency():
    direct = _pins(BOT / "requirements.txt")
    locked = _pins(BOT / "requirements.lock")
    assert direct, "requirements.txt names no dependency -- the tripwire read nothing"
    missing = {name: version for name, version in direct.items()
               if locked.get(name) != version}
    assert not missing, (
        f"the lock does not carry the direct pins: {missing}. The lock is "
        f"what the image installs, so a direct pin that is not in it is a "
        f"requirement that only holds by accident.")


def test_every_entry_in_the_lock_is_pinned():
    locked = _pins(BOT / "requirements.lock")
    assert len(locked) > 3, (
        f"the lock holds {len(locked)} packages -- a lock of three entries "
        f"is the file it was supposed to replace")


def test_the_image_installs_the_lock():
    dockerfile = (ROOT / "telegram-bot" / "Dockerfile").read_text(encoding="utf-8")
    installs = [line for line in dockerfile.splitlines()
                if line.strip().startswith("RUN pip install")]
    assert installs, "the Dockerfile installs nothing -- the tripwire read nothing"
    assert any("requirements.lock" in line for line in installs), (
        f"the image installs {installs}; it has to install the lock, or the "
        f"lock is a document about a build that does not happen")


def test_the_lock_was_resolved_on_the_version_the_image_runs():
    """The lock header names the Python it was resolved on and calls it
    "the version the image runs". The Dockerfile names the Python the
    image runs. If those two drift, the header's claim is false -- and a
    resolution made on another interpreter is not necessarily the
    resolution this image would get (the pins a package offers depend
    on the interpreter, and the set of installable versions does too).

    The claim was unchecked until now: the header said 3.11, the image
    said 3.11, and nobody compared them. A regeneration on a laptop
    with another Python would have left the header saying something the
    image did not do -- a claim nobody would have caught.
    """
    dockerfile = (BOT / "Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"^FROM\s+python:(\d+\.\d+)", dockerfile, re.M)
    assert base, (
        "the Dockerfile does not name a python base image -- the tripwire "
        "read nothing")
    header = (BOT / "requirements.lock").read_text(encoding="utf-8")
    resolved = re.search(r"on Python (\d+\.\d+)", header)
    assert resolved, (
        "the lock does not name the Python it was resolved on -- the "
        "tripwire read nothing")
    assert resolved.group(1) == base.group(1), (
        f"the lock was resolved on Python {resolved.group(1)}, the image "
        f"runs Python {base.group(1)}: the header's claim is false, and "
        f"the resolution is not the one the image would get")
