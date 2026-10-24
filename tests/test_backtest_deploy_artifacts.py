"""Backtests for tests/test_deploy_artifacts.py.

Rule from the T60 wave: "Zelyonyi, no green by accident." A tripwire that
never fires is worse than no tripwire — it reads as coverage.

Each test below mutates a *copy* of update.sh in a temp dir, reintroduces
the historical bug, and asserts the matching tripwire fails. The original
repo file is never touched.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


def _materialise(tmp_path: Path, mutate=None) -> Path:
    """Copy the repo skeleton into tmp_path and optionally mutate update.sh.

    The skeleton must be enough for pytest to COLLECT the tripwire file,
    so the temp tree carries pytest.ini + conftest.py + the test modules
    themselves — not just the scripts they inspect.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "telegram-bot").mkdir()
    (root / "systemd").mkdir()
    (root / "tests").mkdir()
    shutil.copy(UPDATE_SH, root / "scripts" / "update.sh")
    shutil.copy(REPO_ROOT / "scripts" / "install.sh", root / "scripts" / "install.sh")
    for unit in (REPO_ROOT / "systemd").glob("*.service"):
        shutil.copy(unit, root / "systemd" / unit.name)
    for src in (REPO_ROOT / "telegram-bot").glob("*.py"):
        shutil.copy(src, root / "telegram-bot" / src.name)
    # Files pytest needs to run a single test id from the temp tree.
    shutil.copy(REPO_ROOT / "pytest.ini", root / "pytest.ini")
    for extra in ("conftest.py", "test_deploy_artifacts.py"):
        src = REPO_ROOT / "tests" / extra
        if src.exists():
            shutil.copy(src, root / "tests" / extra)
    if mutate:
        text = (root / "scripts" / "update.sh").read_text()
        (root / "scripts" / "update.sh").write_text(mutate(text))
    return root


def _run_one(test_id: str, root: Path) -> subprocess.CompletedProcess:
    """Run a single tripwire test against the mutated tree."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         f"tests/test_deploy_artifacts.py::{test_id}",
         "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(root), capture_output=True, text=True,
    )
    return proc


# ---------------------------------------------------------------------------
# Backtest 1: reintroduce the 4-of-17 partial copy.
# ---------------------------------------------------------------------------
def test_backtest_partial_file_copy_is_caught(tmp_path):
    mutant = '''sudo cp telegram-bot/bot.py "$BOT_DIR/bot.py"
sudo cp telegram-bot/Dockerfile "$BOT_DIR/Dockerfile"
sudo cp telegram-bot/requirements.txt "$BOT_DIR/requirements.txt"
'''
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_update_syncs_whole_tree_not_hand_picked_files", root)
    assert proc.returncode != 0, (
        "the partial-copy tripwire did NOT fire when a hand-listed cp was "
        "reintroduced — it is not protecting anything"
    )


def test_backtest_personal_tools_copy_is_caught(tmp_path):
    mutant = 'sudo cp telegram-bot/personal_tools/bash_command.py "$BOT_DIR/x.py"\n'
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_update_syncs_whole_tree_not_hand_picked_files", root)
    assert proc.returncode != 0, "cp of personal_tools/ was not caught"


# ---------------------------------------------------------------------------
# Backtest 2: reintroduce the generic-compose clobber.
# ---------------------------------------------------------------------------
def test_backtest_generic_compose_clobber_is_caught(tmp_path):
    mutant = (
        'sudo cp telegram-bot/docker-compose.yml "$BOT_DIR/docker-compose.yml"\n'
    )
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_update_does_not_copy_generic_compose_over_production", root)
    assert proc.returncode != 0, (
        "clobbering the production compose was not caught"
    )


# ---------------------------------------------------------------------------
# Backtest 3: reintroduce a bare `docker compose`.
# ---------------------------------------------------------------------------
def test_backtest_bare_compose_invocation_is_caught(tmp_path):
    mutant = "sudo docker compose up -d telegram-bot\n"
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_every_compose_invocation_passes_explicit_file", root)
    assert proc.returncode != 0, (
        "a `docker compose` without -f was not caught — the tripwire is blind"
    )


# ---------------------------------------------------------------------------
# Backtest 4: reintroduce a hardcoded container name.
# ---------------------------------------------------------------------------
def test_backtest_hardcoded_container_name_is_caught(tmp_path):
    mutant = "sudo docker logs tb-bot --tail 20\n"
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_update_has_no_hardcoded_container_names", root)
    assert proc.returncode != 0, "hardcoded tb-bot was not caught"


# ---------------------------------------------------------------------------
# Backtest 5: reintroduce a hardcoded volume name.
# ---------------------------------------------------------------------------
def test_backtest_hardcoded_volume_name_is_caught(tmp_path):
    mutant = 'sudo docker volume inspect bot_data >/dev/null\n'
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_update_has_no_hardcoded_volume_names", root)
    assert proc.returncode != 0, (
        "a literal bot_data volume reference was not caught — this is the "
        "one that silently orphans conversation history"
    )


# ---------------------------------------------------------------------------
# Backtest 6: reintroduce --force-recreate.
# ---------------------------------------------------------------------------
def test_backtest_force_recreate_is_caught(tmp_path):
    mutant = 'run sudo "${DOCKER_COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE"\n'
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_no_bare_force_recreate_without_build", root)
    assert proc.returncode != 0, "--force-recreate was not caught"


# ---------------------------------------------------------------------------
# Backtest 7: reintroduce the swallow-the-error success path.
# ---------------------------------------------------------------------------
def test_backtest_warn_without_exit1_is_caught(tmp_path):
    """Strip the `exit 1` from the error branch — the old script's behaviour."""
    def mutate(text: str) -> str:
        text = text.replace(
            '''        echo "    cd $(git rev-parse --show-toplevel) && git checkout $PREV_HEAD && bash scripts/update.sh"
        exit 1''',
            '''        echo "    (swallowed)"''',
        )
        return text
    root = _materialise(tmp_path, mutate)
    proc = _run_one("test_verify_checks_the_real_container_and_fails", root)
    assert proc.returncode != 0, (
        "a deploy that warns about a broken log but exits 0 was not caught"
    )


# ---------------------------------------------------------------------------
# Backtest 8: reintroduce a syntax error in the script.
# ---------------------------------------------------------------------------
def test_backtest_bash_syntax_error_is_caught(tmp_path):
    def mutate(text: str) -> str:
        return text + '\nif [ "unclosed"\n'
    root = _materialise(tmp_path, mutate)
    proc = _run_one("test_update_sh_is_valid_bash", root)
    assert proc.returncode != 0, "a bash syntax error was not caught"


# ---------------------------------------------------------------------------
# Backtest 9: reintroduce the ordering bug the DRY_RUN rehearsal caught.
# ---------------------------------------------------------------------------
def test_backtest_resolve_after_migration_is_caught(tmp_path):
    """Move resolve_compose to AFTER the migration block — the real bug."""
    def mutate(text: str) -> str:
        # delete the standalone resolve calls that appear before migration
        text = text.replace("resolve_compose\nresolve_container_name\n\n", "", 1)
        return text
    root = _materialise(tmp_path, mutate)
    proc = _run_one("test_compose_is_resolved_before_anything_reads_it", root)
    assert proc.returncode != 0, (
        "moving resolve_compose after the migration block was NOT caught — "
        "the ordering tripwire is blind"
    )


# ---------------------------------------------------------------------------
# Backtest 10: reintroduce the secret-leaking diff.
# ---------------------------------------------------------------------------
def test_backtest_env_diff_leak_is_caught(tmp_path):
    mutant = 'sudo diff "$BOT_DIR/.env" "$BOT_DIR/.env.example" || true\n'
    root = _materialise(tmp_path, lambda t: mutant + "\n" + t)
    proc = _run_one("test_env_diff_does_not_leak_secrets", root)
    assert proc.returncode != 0, (
        "a `diff .env .env.example` that prints secrets was not caught"
    )


# ---------------------------------------------------------------------------
# Backtest 11: remove the compose-parse guard.
# ---------------------------------------------------------------------------
def test_backtest_compose_parse_guard_removal_is_caught(tmp_path):
    def mutate(text: str) -> str:
        return text.replace("cannot parse", "note: parse")
    root = _materialise(tmp_path, mutate)
    proc = _run_one("test_compose_parse_is_verified", root)
    assert proc.returncode != 0, (
        "removing the unparseable-compose guard was not caught"
    )


# ---------------------------------------------------------------------------
# Control: the unmutated tree must pass. Without this, a tripwire that
# fails on everything would satisfy every backtest above.
# ---------------------------------------------------------------------------
def test_control_unmutated_tree_passes(tmp_path):
    root = _materialise(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_deploy_artifacts.py",
         "-q", "--tb=line", "-p", "no:cacheprovider"],
        cwd=str(root), capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "the tripwire file fails on the CURRENT update.sh — the tripwires "
        "are over-constrained:\n" + proc.stdout[-3000:]
    )