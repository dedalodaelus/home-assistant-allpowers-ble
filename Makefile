.PHONY: all clean coverage format lint release test validate

all: lint coverage
	$(MAKE) clean
	$(MAKE) validate

format:
	ruff check --fix custom_components scripts tests
	ruff format custom_components scripts tests

lint:
	ruff format --check custom_components scripts tests
	ruff check custom_components scripts tests
	mypy custom_components/allpowers_ble
	pylint --errors-only custom_components/allpowers_ble

test:
	USE_REAL_HOMEASSISTANT=0 pytest --ignore=tests/homeassistant

coverage:
	USE_REAL_HOMEASSISTANT=0 pytest --ignore=tests/homeassistant \
		--cov=custom_components/allpowers_ble \
		--cov-branch \
		--cov-report=term-missing \
		--cov-report=xml

validate:
	python scripts/validate_repository.py

release:
	python scripts/build_release.py --clean

clean:
	rm -rf .coverage .mypy_cache .pytest_cache .ruff_cache \
		__pycache__ dist htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
