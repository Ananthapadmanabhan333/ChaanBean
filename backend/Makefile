.PHONY: up down init seed demo test lint api worker check reset

up:            ## start postgres and redis
	docker compose up -d postgres redis

down:
	docker compose down

init:          ## create the schema, apply RLS, provision roles
	python -m app.db init

check:         ## verify connectivity, RLS coverage and role attributes
	python -m app.db check

seed:          ## realistic Indian buyer data, including refusable cases
	python -m app.seed

demo:          ## run a campaign end to end against the stub carrier
	python -m app.demo

api:
	uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

worker:
	python -m app.worker

test:
	pytest -q

lint:
	lint-imports

reset:         ## drop everything and rebuild from scratch (local only)
	python -m app.db drop && python -m app.db init && python -m app.seed
