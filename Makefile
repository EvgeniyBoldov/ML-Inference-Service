RELEASE_ENV ?= release.env

.PHONY: release-preview release base-hash test test-delivery runtime-base-preview runtime-base-release

release-preview:
	@./scripts/release.sh preview $(RELEASE_ENV)

release:
	@./scripts/release.sh publish $(RELEASE_ENV)

base-hash:
	@bash -c 'source scripts/release-common.sh; base_input_sha'

test: test-delivery
	@cd apps/inference-service && python3 -m pytest -q

test-delivery:
	@python3 -m unittest discover -s scripts/tests -q
	@PYTHONPATH=apps/inference-service python3 -m unittest discover -s apps/inference-service/tests -p test_delivery_logging.py -q

# Compatibility aliases: dependencies and application code are released together.
runtime-base-preview: release-preview
runtime-base-release: release
