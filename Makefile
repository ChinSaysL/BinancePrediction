.PHONY: help install test record paper analyze harvest backtest backtest-models backtest-candle stop config config-status config-list

# Live host and transport. Override on the command line (make config VPS_SSH=...)
VPS ?= root@5.104.81.124
VPS_SSH_KEY ?= ~/.ssh/id_ed25519
SSH = ssh -i $(VPS_SSH_KEY) -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
RSYNC = rsync -a --exclude 'data/*.db*' --exclude 'data/*.log' -e '$(SSH)'
# Is this already the live box (where systemd manages the bot)? If so the same
# targets run in place -- ssh-ing from the VPS back to itself just fails with a
# missing-key error that looks like a broken profile.
LIVE_HERE := $(shell systemctl list-unit-files btcpredict.service 2>/dev/null | grep -q btcpredict && echo 1)

help:
	@echo "make install   - install dependencies"
	@echo "make test      - run the test suite"
	@echo "make record    - Phase 0: record only, no orders (default mode)"
	@echo "make paper     - Phase 2: paper trading against live feeds"
	@echo "make analyze   - Phase 1: reports from recorded data"
	@echo "make backtest  - validate the model on kline history"
	@echo "make backtest-models - head-to-head comparison of all pricing models"
	@echo "make backtest-candle - replay the candle-open gate on recorded quotes"
	@echo "make harvest   - pull settled rounds as oracle ground truth"
	@echo "make stop      - stop a background recorder"
	@echo "make config-list   - saved live profiles (configs/*.env)"
	@echo "make config-status - which profile the live .env matches"
	@echo "make config PROFILE=optimal|aggressive - switch live + restart"

install:
	pip install -r requirements.txt

test:
	python -m pytest tests/ -q

record:
	python -m btcpredict.main --mode record

paper:
	python -m btcpredict.main --mode paper

analyze:
	python -m tools.analyze

backtest:
	python -m tools.backtest_calibration --days 10

backtest-models:
	python -m tools.backtest_models --days 10

backtest-candle:
	python -m tools.backtest_candle_open --model digital

harvest:
	python -m tools.harvest --rounds 300

stop:
	@pkill -f "btcpredict.main" && echo "recorder stopped" || echo "no recorder running"

# --- live config profiles ---------------------------------------------------
# One command to move the live bot between saved strategies. The backups the
# switch leaves behind (.env.bak-<utc>-pre-<profile>) are the revert path.
config-list:
	@ls -1 configs/*.env 2>/dev/null | sed 's|configs/||;s|\.env$$||' || echo "no profiles in configs/"

config-status:
	$(if $(LIVE_HERE),.venv/bin/python -m tools.switch_env --status,$(SSH) $(VPS) 'cd /root/BinancePrediction && .venv/bin/python -m tools.switch_env --status')

config:
	@test -n "$(PROFILE)" || { echo "set PROFILE=<name>, one of:"; ls -1 configs/*.env | sed 's|configs/||;s|\.env$$||'; exit 1; }
	$(if $(LIVE_HERE),,$(RSYNC) configs tools $(VPS):/root/BinancePrediction/)
	$(if $(LIVE_HERE),.venv/bin/python -m tools.switch_env $(PROFILE) --restart,$(SSH) $(VPS) 'cd /root/BinancePrediction && .venv/bin/python -m tools.switch_env $(PROFILE) --restart')
