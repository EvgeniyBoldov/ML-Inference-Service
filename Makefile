RELEASE_ENV ?= release.env
DOCKERFILE ?= apps/inference-service/Dockerfile
BUILD_CONTEXT ?= apps/inference-service

.PHONY: release-preview release docker-build docker-push test

release-preview:
	@./scripts/release.sh preview $(RELEASE_ENV)

release:
	@./scripts/release.sh prepare $(RELEASE_ENV)
	@$(MAKE) docker-build docker-push

docker-build:
	@set -a; . ./$(RELEASE_ENV); set +a; \
		docker build --pull \
			--build-arg RELEASE_VERSION="$$RELEASE_VERSION" \
			--build-arg SOURCE_COMMIT="$$RELEASE_COMMIT" \
			--tag "$$REGISTRY/$$IMAGE:$$RELEASE_VERSION" \
			--tag "$$REGISTRY/$$IMAGE:$$RELEASE_COMMIT" \
			--file "$(DOCKERFILE)" "$(BUILD_CONTEXT)"

docker-push:
	@set -a; . ./$(RELEASE_ENV); set +a; \
		docker push "$$REGISTRY/$$IMAGE:$$RELEASE_VERSION"; \
		docker push "$$REGISTRY/$$IMAGE:$$RELEASE_COMMIT"

test:
	@cd apps/inference-service && python3 -m pytest -q
