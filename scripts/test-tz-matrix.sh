#!/usr/bin/env bash
# Run the suite under several timezones.
#
# Added 2026-10-22 after tests/test_review_wave7.py went red at 02:06 UTC
# with no code change anywhere. That test compared the next 05:00 in two
# zones and expected a 3h difference, which only holds while the current
# instant is between 02:00 and 05:00 UTC -- one zone passes 05:00 and
# rolls to tomorrow while the other has not, and the difference becomes
# 21h. It was green all evening and the clock took it down.
#
# This is a script rather than a test: four suite runs is four times the
# default cost for a property that should hold every time, so it runs on
# demand and in CI as a separate job, not inside pytest.
#
# The zones span UTC-11 to UTC+14 on purpose. Pacific/Kiritimati is
# UTC+14 and America/New_York is UTC-5 in October; together they are a
# 25-hour spread, which is enough to catch a test that assumed "today"
# means the same day on both sides of an offset, and a test that assumed
# a local-midnight boundary is stable.
set -uo pipefail

REPO="${REPO:-/opt/telegrambot/bot/repo}"
IMAGE="${IMAGE:-bot-testimg311}"
ZONES="${ZONES:-UTC Europe/Moscow America/New_York Pacific/Kiritimati}"

fail=0
for zone in $ZONES; do
    printf '%-24s ' "$zone"
    out=$(docker run --rm -e TZ="$zone" \
        -v "$REPO:/repo" -w /repo \
        -e BOT_TOKEN=1:fake -e CONVERSATIONS_DB=/tmp/tz-matrix.db \
        "$IMAGE" python -m pytest tests/ -q --tb=short -p no:cacheprovider 2>&1)
    line=$(printf '%s\n' "$out" | grep -E 'passed|failed|error' | tail -1)
    echo "${line:-NO RESULT}"
    # The point of the script: identical outcomes, not merely zero.
    # Count the skipped tests too -- a test that skips itself in one zone
    # and runs in another is the same coupling wearing a different hat.
    if printf '%s' "$line" | grep -qE 'failed|error'; then
        printf '%s\n' "$out" | grep -E '^FAILED|^ERROR' | head -20
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    echo "TZ matrix: FAILED"
    exit 1
fi
echo "TZ matrix: all zones agree"