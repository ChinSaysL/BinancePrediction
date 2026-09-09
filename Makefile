.PHONY: help install test record paper analyze harvest backtest backtest-models backtest-candle stop

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
