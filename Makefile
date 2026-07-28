.PHONY: up down logs ps test validate

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

test:
	cd exporter && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v

validate:
	cd exporter && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
	IOMETER_HOST=iometer.local docker compose config --quiet
	python3 -m json.tool grafana/dashboards/iometer-overview.json >/dev/null
	python3 -m json.tool grafana/dashboards/iometer-energy.json >/dev/null
	python3 -m json.tool grafana/dashboards/iometer-health.json >/dev/null
