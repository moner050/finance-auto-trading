#!/bin/sh
# Run the daily capture, once per UTC day, forever.
#
# The put-call ratio is the reason this exists. Deribit publishes no history of
# it, so a day this container is not running is a day that series can never
# have. Breadth and volatility are rebuilt from klines whenever backfill runs;
# the put-call reading is only ever available on the day itself.
#
# A failed run does not stop the loop. One unreachable venue costs one day of
# one measure, and exiting would cost every day after it as well.
set -eu

: "${CAPTURE_HOUR:=1}"

while true; do
	now_hour=$(date -u +%-H)
	now_minute=$(date -u +%-M)
	# Seconds until the next occurrence of CAPTURE_HOUR:00 UTC. Run after the
	# UTC day it measures has closed, not at the boundary, so a venue that is
	# a few minutes late still counts.
	wait_hours=$(((CAPTURE_HOUR - now_hour + 24) % 24))
	if [ "$wait_hours" -eq 0 ] && [ "$now_minute" -gt 5 ]; then
		wait_hours=24
	fi
	sleep_for=$((wait_hours * 3600 - now_minute * 60))
	if [ "$sleep_for" -lt 60 ]; then
		sleep_for=60
	fi
	echo "capture: sleeping ${sleep_for}s until ${CAPTURE_HOUR}:00 UTC"
	sleep "$sleep_for"

	echo "capture: running for $(date -u +%F)"
	if python -m autotrader.apps.capture; then
		echo "capture: recorded"
	else
		# Reported, not fatal. Tomorrow is still worth measuring.
		echo "capture: failed; the loop continues" >&2
	fi
done
