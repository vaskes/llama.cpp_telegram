"""The whisper-api compose describes the service that actually runs.

Three claims about that compose were wrong, and all three were found by
measuring the running container rather than by reading the file:

1. **The bind.** The service runs on host network, so the process
   listened on 0.0.0.0:8000 -- every process on the host could reach
   the transcription API. The only consumer is the bot, on the same
   host, at http://127.0.0.1:8000 (WHISPER_URL in the bot's .env), so
   the bind is loopback-only now.

2. **The image.** `latest-cpu` is a moving target: a rebuild a month
   later pulls a different engine than the one the deployment was
   measured against. The compose names the digest that runs.

3. **The model variable.** The server reads its settings through
   pydantic-settings with `env_nested_delimiter="__"`, so the section
   is WHISPER and the field is MODEL: the variable is
   `WHISPER__MODEL`. The compose used to say `WHISPER_MODEL`, which
   the server never read -- it loaded the image default
   (Systran/faster-whisper-small), while the file claimed
   faster-distil-whisper-large-v3.

From the external review of 2026-10-28 (finding #1 and the bind nit).
"""
import pathlib
import re

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "whisper-api" / "docker-compose.yml"


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _environment() -> dict:
    """The environment the compose actually sets.

    Parsed, not grepped: the file's comments mention both
    `UVICORN_HOST` and `WHISPER_MODEL` while explaining what they used
    to say, and a tripwire that matches the prose reports the comment
    as the setting. The property is the value the container gets.
    """
    env: dict = {}
    in_block = False
    for line in _compose().splitlines():
        stripped = line.strip()
        if not in_block:
            if stripped.startswith("environment:"):
                in_block = True
            continue
        if stripped.startswith("#"):
            # A comment inside the block explains the setting; it is not
            # the setting.
            continue
        if not stripped or not stripped.startswith("-"):
            break
        entry = stripped.lstrip("- ").strip()
        if "=" in entry:
            key, value = entry.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def test_the_transcription_api_listens_on_loopback_only():
    env = _environment()
    assert env, "the compose sets no environment -- the tripwire read nothing"
    assert env.get("UVICORN_HOST") == "127.0.0.1", (
        f"the transcription API binds {env.get('UVICORN_HOST')!r}; on host "
        f"network that is every interface of the host")
    assert "0.0.0.0" not in env.values(), (
        f"the compose still binds 0.0.0.0: {env}")


def test_the_image_is_pinned_by_digest():
    text = _compose()
    image = re.search(r"^\s*image:\s*(\S+)", text, re.M)
    assert image, "the compose has no image line"
    ref = image.group(1)
    assert "@sha256:" in ref, (
        f"the image is {ref} -- a tag moves under you; pin the digest")


def test_the_model_variable_is_the_one_the_server_reads():
    env = _environment()
    assert "WHISPER__MODEL" in env, (
        "the compose does not name the model through WHISPER__MODEL -- "
        "the server reads WHISPER__MODEL (pydantic-settings, nested "
        f"delimiter '__'), not WHISPER_MODEL; the compose sets {env}")
    assert "WHISPER_MODEL" not in env, (
        "the compose still sets WHISPER_MODEL, which the server never "
        f"reads: {env}")
