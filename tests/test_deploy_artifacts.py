"""T64 tripwires for the deploy artifacts (scripts/update.sh, scripts/install.sh).

Why this file exists
--------------------
`scripts/update.sh` shipped for months with a four-file copy list
(bot.py, Dockerfile, requirements.txt, docker-compose.yml) while the
repo carries 13 python modules plus personal_tools/. Every deploy
therefore mixed a new bot.py with stale dispatch.py / handlers.py /
call_llama.py. T53 and T60 made `chat_type` a required keyword-only
argument and T62 made dispatch.py do `from handlers import (...)`, so a
mixed build dies with ImportError or TypeError on the first message.

Three waves of code review had already found this bug class three times
in application code (T59 S40 NameError, 327b7b0 handle_photo NameError,
T60 fetch_tools_from_llama bare name). None of them looked at the deploy
script, because the review reads .py files and this failure lives in .sh.

The same script also hardcoded the container name `telegram-bot` while
production names it `tb-bot`, and copied the generic compose over the
production one. These tripwires make all of that structural: a future
edit cannot reintroduce a partial sync or a host-specific literal
without failing a test.

Every tripwire here has a backtest in test_backtest_deploy_artifacts.py.
"""
import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
TELEGRAM_BOT = REPO_ROOT / "telegram-bot"

# Container names that belong to a *specific deployment*. update.sh must
# resolve the name from compose at runtime instead of hardcoding either.
HOST_SPECIFIC_NAMES = ("tb-bot", "tb-sandbox-agent", "tb-openapi-proxy")


@pytest.fixture(scope="module")
def update_src() -> str:
    return UPDATE_SH.read_text()


@pytest.fixture(scope="module")
def update_lines() -> list[str]:
    return UPDATE_SH.read_text().splitlines()


# ---------------------------------------------------------------------------
# 1. The core tripwire: no hand-listed copy of a subset of the bot tree.
# ---------------------------------------------------------------------------
def test_update_syncs_whole_tree_not_hand_picked_files(update_src):
    """update.sh must not cp individual .py modules one by one.

    The historical bug was `cp telegram-bot/bot.py`, `cp ...Dockerfile`,
    `cp ...requirements.txt` — and nothing else. Any `cp telegram-bot/*.py`
    or `cp .../<module>.py` line is the same bug wearing a different hat.
    """
    offenders = [
        (i, ln.strip())
        for i, ln in enumerate(update_src.splitlines(), 1)
        # cp/mv of a specific python file out of telegram-bot/
        if re.search(r"\b(?:cp|mv)\s+.*telegram-bot/\S+\.py\b", ln)
        # or out of personal_tools/
        or re.search(r"\b(?:cp|mv)\s+.*personal_tools/", ln)
    ]
    assert not offenders, (
        "update.sh copies individual python modules by name — that is how "
        "12 of 13 modules stayed stale in production. Sync the whole "
        "telegram-bot/ tree instead (rsync -a --delete or cp -r loop):\n  "
        + "\n  ".join(f"L{i}: {t}" for i, t in offenders)
    )


def test_update_does_not_copy_generic_compose_over_production(update_src):
    """Coping docker-compose.yml into $BOT_DIR would replace the production file."""
    offenders = [
        (i, ln.strip())
        for i, ln in enumerate(update_src.splitlines(), 1)
        if re.search(r"\b(?:cp|mv)\s+.*docker-compose\.yml\b", ln)
    ]
    assert not offenders, (
        "update.sh copies a compose file into $BOT_DIR by name. On a "
        "production host that overwrites docker-compose.production.yml "
        "with the generic layout (tb-bot -> telegram-bot, tb_bot_data -> "
        "bot_data), orphaning the SQLite conversation history:\n  "
        + "\n  ".join(f"L{i}: {t}" for i, t in offenders)
    )


def test_every_bot_module_reaches_bot_dir(update_src):
    """The guard in update.sh that checks for missing modules must exist.

    Belt-and-braces on top of tripwire 1: even if someone reintroduces a
    partial sync, the runtime guard aborts before `up -d` instead of
    starting a Frankenstein container.
    """
    assert "missing after sync" in update_src, (
        "update.sh lost its post-sync completeness check. Without it a "
        "partial sync deploys silently instead of aborting."
    )
    assert "refusing to deploy a partial build" in update_src, (
        "the completeness check must abort the deploy, not just warn"
    )


# ---------------------------------------------------------------------------
# 2. Compose is never left to guess.
# ---------------------------------------------------------------------------
def test_every_compose_invocation_passes_explicit_file(update_lines):
    """`docker compose` with no -f reads docker-compose.yml — the generic one."""
    offenders = []
    for i, ln in enumerate(update_lines, 1):
        stripped = ln.strip()
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        if "docker compose" not in stripped:
            continue
        # Allowed: the assignment that builds the array, and commented docs.
        if "${DOCKER_COMPOSE[@]}" in stripped:
            continue
        if re.search(r"docker\s+compose\s+-f\s+", stripped):
            continue
        offenders.append((i, stripped))
    assert not offenders, (
        "docker compose invoked without an explicit -f. It defaults to "
        "docker-compose.yml (generic layout) and will silently pick the "
        "wrong container/volume names:\n  "
        + "\n  ".join(f"L{i}: {t}" for i, t in offenders)
    )


def test_compose_file_is_resolved_at_runtime(update_src):
    assert "resolve_compose" in update_src
    assert "docker-compose.production.yml" in update_src, (
        "update.sh must prefer the production compose when the host has one"
    )


# ---------------------------------------------------------------------------
# 3. No host-specific literals.
# ---------------------------------------------------------------------------
def test_update_has_no_hardcoded_container_names(update_lines):
    offenders = []
    for i, ln in enumerate(update_lines, 1):
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        for name in HOST_SPECIFIC_NAMES:
            # allow inside the "these are wrong, do not use" comment block
            if name in stripped and "HOST_SPECIFIC" not in stripped:
                offenders.append((i, name, stripped))
    assert not offenders, (
        "update.sh hardcodes a deployment-specific container name. Generic "
        "and production layouts disagree, so the name must be read from "
        "compose:\n  "
        + "\n  ".join(f"L{i} [{n}]: {t}" for i, n, t in offenders)
    )


def test_container_name_comes_from_compose(update_src):
    assert "resolve_container_name" in update_src, (
        "container name must be resolved from the compose file"
    )
    assert "container_name" in update_src


def test_update_has_no_hardcoded_lan_ips(update_lines):
    """The T63 lesson: 192.168.10.x in a committed file outlives the host."""
    offenders = [
        (i, ln.strip())
        for i, ln in enumerate(update_lines, 1)
        if re.search(r"\b192\.168\.\d+\.\d+\b", ln) and not ln.strip().startswith("#")
    ]
    assert not offenders, (
        "update.sh contains a hardcoded LAN IP — endpoint addresses belong "
        "in .env:\n  " + "\n  ".join(f"L{i}: {t}" for i, t in offenders)
    )


def test_update_has_no_hardcoded_volume_names(update_lines):
    """bot_data vs tb_bot_data is the conversation-history trap."""
    offenders = []
    for i, ln in enumerate(update_lines, 1):
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        for vol in ("bot_data", "tb_bot_data"):
            # allow when read into a shell variable from compose
            if re.search(rf'\b{vol}\b', stripped) and "DATA_VOLUME=" not in stripped:
                offenders.append((i, vol, stripped))
    assert not offenders, (
        "update.sh names a Docker volume literally. The volume name decides "
        "whether the SQLite history is found or orphaned — read it from "
        "compose:\n  "
        + "\n  ".join(f"L{i} [{v}]: {t}" for i, v, t in offenders)
    )


# ---------------------------------------------------------------------------
# 4. --build discipline (the T62 Makefile lesson, applied to update.sh).
# ---------------------------------------------------------------------------
def test_no_bare_force_recreate_without_build(update_src):
    """`up -d --force-recreate` recreates from the STALE image."""
    offenders = []
    for i, ln in enumerate(update_src.splitlines(), 1):
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        if "--force-recreate" in stripped:
            offenders.append((i, stripped))
    assert not offenders, (
        "--force-recreate recreates the container from whatever image is "
        "already tagged, so a .py change never lands and the update looks "
        "like a successful no-op:\n  "
        + "\n  ".join(f"L{i}: {t}" for i, t in offenders)
    )


def test_build_precedes_up(update_src):
    body = update_src
    build_at = body.find('build "$SERVICE"')
    up_at = body.find('up -d --no-deps "$SERVICE"')
    assert build_at != -1, "update.sh must run an explicit build"
    assert up_at != -1, "update.sh must run an explicit up"
    assert build_at < up_at, "build must come before up -d"


# ---------------------------------------------------------------------------
# 5. Failure surfaces instead of printing [OK].
# ---------------------------------------------------------------------------
def test_verify_checks_the_real_container_and_fails(update_src):
    """The old script logged a warning and still exited 0."""
    assert 'docker logs "$CONTAINER_NAME"' in update_src, (
        "the post-deploy log check must target the resolved container name"
    )
    # The error branch must exit non-zero, not just print.
    assert re.search(r"warn\s+\"bot log contains errors[\s\S]{0,400}?exit 1", update_src), (
        "a deploy whose log shows ImportError/TypeError must exit non-zero, "
        "not print a warning and claim success"
    )


def test_error_scan_covers_import_errors(update_src):
    """T62's failure mode was ImportError, not just NameError."""
    for needle in ("Traceback", "TypeError", "SyntaxError", "ImportError"):
        assert needle in update_src, f"log error scan missing {needle}"


# ---------------------------------------------------------------------------
# 6. The script is at least syntactically valid shell.
# ---------------------------------------------------------------------------
def test_update_sh_is_valid_bash(update_src, tmp_path):
    """T-rule: never commit a bash script that has not been parsed.

    Mavis mistake #20 — a script committed on the strength of one
    hand-verified output line. `bash -n` costs nothing and catches the
    quoting class of bug before it reaches a production host.
    """
    script = tmp_path / "update.sh"
    script.write_text(update_src)
    proc = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        f"update.sh is not valid bash: {proc.stderr.strip()}"
    )


def test_install_sh_is_valid_bash(tmp_path):
    """install.sh has the same delivery-vehicle risk as update.sh."""
    script = tmp_path / "install.sh"
    script.write_text(INSTALL_SH.read_text())
    proc = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        f"install.sh is not valid bash: {proc.stderr.strip()}"
    )


# ---------------------------------------------------------------------------
# 7. install.sh shares the compose-clobbering bug.
# ---------------------------------------------------------------------------
def test_install_also_does_not_clobber_production_compose():
    """install.sh must not let a re-run swap the production compose for the generic one.

    Note the scope: copying *whisper-api*'s compose by name into WHISPER_DIR
    is fine — whisper has one compose file and no production variant. What
    must not happen is the *bot's* generic compose overwriting the bot's
    production compose on a re-run.
    """
    src = INSTALL_SH.read_text()
    offenders = [
        (i, ln.strip())
        for i, ln in enumerate(src.splitlines(), 1)
        # only the BOT's compose, not whisper's
        if re.search(r"\b(?:cp|mv)\s+.*telegram-bot/docker-compose\.yml\b", ln)
    ]
    assert not offenders, (
        "install.sh copies the bot's generic compose into $BOT_DIR by name — "
        "on a re-run that overwrites docker-compose.production.yml and orphans "
        "the SQLite conversation history:\n  "
        + "\n  ".join(f"L{i}: {t}" for i, t in offenders)
    )
    # The deploy-artifact ambiguity must be surfaced to the operator, since
    # bare `docker compose` on a host with both files picks the generic one.
    assert "docker-compose.production.yml" in src, (
        "install.sh should tell the operator which compose to use when both "
        "files are present"
    )


def test_compose_is_resolved_before_anything_reads_it(update_src):
    """T64 wave-trap #8: resolve_compose() ran AFTER the migration check.

    The migration block called `docker compose -f "$COMPOSE_FILE"
    config --volumes` while COMPOSE_FILE was still empty, so the command
    failed, DATA_VOLUME came out empty, and the script printed
    "compose declares no named volume — nothing to migrate" for a compose
    that declares tb_bot_data. A silent no-op that reads like a clean
    bill of health — the exact wave-trap shape (claim vs implementation)
    we have now hit eight times.

    An AST-level ordering assertion is brittle in bash; this checks the
    textual invariant that actually broke: the first read of $COMPOSE_FILE
    must come after resolve_compose is defined AND before any consumer.
    """
    resolve_at = update_src.find("resolve_compose()")
    assert resolve_at != -1, "resolve_compose() is never invoked"

    # The migration block must not be the first thing to touch $COMPOSE_FILE
    # before resolution. Guard the specific consumer that broke.
    migrate_at = update_src.find("check data migration")
    first_compose_read = update_src.find('config --volumes')
    assert migrate_at != -1 and first_compose_read != -1

    # The resolve+assign call must appear before the migration block runs.
    call_at = update_src.find("\nresolve_compose\n")
    assert call_at != -1, (
        "update.sh must call resolve_compose as a statement (on its own "
        "line) before the migration check consumes $COMPOSE_FILE"
    )
    assert call_at < migrate_at, (
        "resolve_compose must run BEFORE the data-migration block reads "
        "$COMPOSE_FILE, otherwise the migration check silently no-ops"
    )


def test_compose_parse_is_verified(update_src):
    """resolve_compose must fail loudly if the compose does not load."""
    assert "cannot parse" in update_src, (
        "resolve_compose must verify the chosen compose parses, otherwise a "
        "broken compose makes every later call a silent no-op"
    )
    # The die must be INSIDE the `if ! docker compose ... config` guard, i.e.
    # the same statement, not merely mentioned somewhere in the file.
    assert re.search(
        r'if\s+!\s+docker compose[^\n]*config[^\n]*\n\s*die\s+"cannot parse',
        update_src,
    ), (
        "the unparseable-compose guard must abort with die() directly inside "
        "the config-check condition, not just mention the string"
    )


def test_env_diff_does_not_leak_secrets(update_src):
    """Never `diff .env .env.example` — that prints BOT_TOKEN to stdout/logs.

    The old script did exactly this. Keys are reported by NAME only.
    """
    offenders = [
        (i, ln.strip())
        for i, ln in enumerate(update_src.splitlines(), 1)
        if re.search(r"diff[^\n]*\$\{?BOT_DIR\}?/\.env\b", ln)
    ]
    assert not offenders, (
        "update.sh diffs the live .env against the example — that prints "
        "BOT_TOKEN / WEBHOOK_SECRET_TOKEN into the terminal and any CI log. "
        "Report missing KEYS by name instead:\n  "
        + "\n  ".join(f"L{i}: {t}" for i, t in offenders)
    )


def test_dry_run_does_not_touch_volumes(update_src):
    """The migration block must not docker run against a real volume under DRY_RUN."""
    migrate = update_src[update_src.find("check data migration"):][:2000]
    dry_branch_at = migrate.find("DRY_RUN")
    assert dry_branch_at != -1, (
        "the migration block must have an explicit DRY_RUN branch"
    )
    # The real seeding must sit AFTER the dry-run early-return inside that block.
    assert migrate.find("alpine:latest cp -a") > dry_branch_at, (
        "the real volume-seeding docker run must be unreachable when DRY_RUN=1"
    )


# ---------------------------------------------------------------------------
# 8. The systemd units point at a bare `docker compose` too.
# ---------------------------------------------------------------------------
def test_systemd_units_do_not_contradict_compose_selection():
    """systemd/*.service hardcode WorkingDirectory and bare `docker compose`.

    They are not on the llmhost1 critical path (that stack is driven by the
    compose files directly), so this is a MEDIUM: the tripwire asserts the
    units don't half-use `-f` (which would be worse than not using it), and
    does not pretend the bare-compose issue is closed there.
    """
    units = list((REPO_ROOT / "systemd").glob("*.service"))
    assert units, "expected systemd units to exist"
    for unit in units:
        body = unit.read_text()
        assert "docker compose" in body
        # A `-f` that is a standalone flag (not part of --force-recreate)
        # on a compose line means the unit half-selects a compose file.
        # That inconsistency is a bug; pick one mode.
        for ln in body.splitlines():
            if "docker compose" not in ln:
                continue
            assert not re.search(r"(?<!\S)-f\s+\S", ln), (
                f"{unit.name}: a compose line uses -f while the unit is "
                f"otherwise generic — half-selecting the compose file is "
                f"worse than not selecting it: {ln.strip()!r}"
            )