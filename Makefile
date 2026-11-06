# slot-machine: develop on the Mac, run on the Pi.
PI      ?= yarikp@yarikos.local
SSH_KEY ?= $(HOME)/.ssh/pi
SSH     := ssh -i $(SSH_KEY) -o IdentitiesOnly=yes -o LogLevel=ERROR
PYTHON  ?= python3
VENV    := .venv
APP     := dist/slot-machine
REMOTE  := .local/bin/slot-machine
LOG     := .local/state/slot-machine/watch.log

.PHONY: help dev test lint fmt build clean deploy cron-on cron-off smoke-pi status logs \
        config edit-config push-config find-chat-id test-telegram set-cookie discover check-now renew-token \
        share-extension signin signin-linux-check

help:
	@echo "On the Mac:"
	@echo "  make dev            create .venv with pytest and ruff"
	@echo "  make test           run the test suite"
	@echo "  make lint           ruff check"
	@echo "  make build          build the single-file zipapp $(APP)"
	@echo "  make config         create/refresh config/slot-machine.env (git-ignored) from the template"
	@echo "  make edit-config    the same, then open it in \$$EDITOR"
	@echo "On the Pi ($(PI)):"
	@echo "  make deploy         build, install the app, upload your settings (cron untouched)"
	@echo "  make push-config    upload only your settings"
	@echo "  make smoke-pi       run the installed app against fake servers"
	@echo "  make find-chat-id   / make test-telegram"
	@echo "  make set-cookie     paste a fresh _yatri_session (hidden prompt)"
	@echo "  make discover       list your schedule and location ids"
	@echo "  make cron-on        checks on CRON_SCHEDULE   (make cron-off: off)"
	@echo "  make check-now      one real check right now (ignores the interval and any pause)"
	@echo "  make renew-token    set up the renewal page's secret (copied for the Chrome extension)"
	@echo "  make share-extension  zip the Chrome extension for someone else, current token on the clipboard"
	@echo "  make status         / make logs"
	@echo "  make signin            Mac Chrome signs in and renews the Pi session"
	@echo "  make signin-linux-check  Linux Chrome in Docker, without opening the visa site"

$(VENV)/bin/pytest: requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -r requirements-dev.txt
	@touch $@

dev: $(VENV)/bin/pytest

test: dev
	$(VENV)/bin/pytest

lint: dev
	$(VENV)/bin/ruff check src tests

fmt: dev
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

build:
	$(PYTHON) deploy/build.py $(APP)

clean:
	rm -rf build dist .pytest_cache .ruff_cache

deploy: build
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh

cron-on:
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh --cron-on

cron-off:
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh --cron-off

smoke-pi:
	PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/smoke-pi.sh

status:
	@$(SSH) $(PI) '$(REMOTE) --status'

logs:
	@$(SSH) $(PI) 'tail -n 50 $(LOG) 2>/dev/null || echo "no log yet (cron writes it)"'

config:
	@$(PYTHON) deploy/sync_config.py

edit-config: config
	@$${EDITOR:-nano} config/slot-machine.env
	@echo "Saved locally. 'make push-config' sends it to the Pi."

push-config:
	@PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh --push-config

find-chat-id:
	@$(SSH) $(PI) '$(REMOTE) --find-chat-id'

test-telegram:
	@$(SSH) $(PI) '$(REMOTE) --test-telegram'

set-cookie:
	@$(SSH) -t $(PI) '$(REMOTE) --set-cookie'

discover:
	@$(SSH) $(PI) '$(REMOTE) --discover'

# One real check now, outside cron. --force skips MIN_INTERVAL_MINUTES and any back-off;
# the run lock still keeps it from overlapping a cron run. Output also goes to the log.
check-now:
	@$(SSH) $(PI) 'set -o pipefail; $(REMOTE) --force --via check-now 2>&1 | tee -a $(LOG)'

# Renewal page secret: new RENEW_TOKEN (and RENEW_URL if empty) in your local settings, copied to the
# clipboard for the Chrome extension, then pushed to the Pi. Rotating it needs the extension updated.
renew-token: config
	@$(PYTHON) deploy/renew_token.py
	@PI=$(PI) SSH_KEY=$(SSH_KEY) deploy/deploy.sh --push-config

# Mac Chrome. Signs in and posts the session. Ctrl+C stops it. Options go on the python
# command: PYTHONPATH=src python3 -m signin --every 50 --jitter 3
signin:
	PYTHONPATH=src $(PYTHON) -m signin

# Real Google Chrome on a virtual screen, the mini-PC stand-in.
# Does not open the visa site. A real cycle is: src/signin/docker/run.sh --every 50 --jitter 3
signin-linux-check:
	src/signin/docker/run.sh --check

# The Chrome extension for another person in TELEGRAM_CHAT_ID: a zip to send them (AirDrop), and the
# current RENEW_TOKEN on the clipboard for its options. Nothing is rotated or pushed.
share-extension:
	@mkdir -p dist && rm -rf dist/slot-machine-extension dist/slot-machine-extension.zip
	@cp -R extension dist/slot-machine-extension
	@cd dist && zip -qr slot-machine-extension.zip slot-machine-extension -x '*/.*' && rm -rf slot-machine-extension
	@echo "Made dist/slot-machine-extension.zip (open it in Finder: open dist)."
	@$(PYTHON) deploy/renew_token.py --copy
