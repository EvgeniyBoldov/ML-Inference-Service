# Build and production delivery

`make release` builds and publishes the inference API image, checks its
dependencies and Alembic head, then commits the API release manifest. It does
not build a model-runtime image: production model containers use versioned
Airflow and Jupyter producer images already loaded onto the production Docker
host.

The API release manifest pins its API image's base image and records the source
commit, API release version, database revision, and blue/green ports. The
production controller stages the manifest and Compose file, pulls only the API
image, verifies its labels and migrations, starts the candidate service, waits
for readiness, and switches Nginx. PostgreSQL remains shared between API
releases.

## Producer images

Configure `ML_INFERENCE_PRODUCER_IMAGE` on Airflow and Jupyter. The model
logging package persists its value as MLflow version tag
`ml_inference.producer_image`. Use a unique tag for each producer image build;
the production server must have that exact image available locally before a
model deployment refers to it. Inference runtime startup uses
`docker run --pull=never` and does not contact a registry or install Python
packages.

Producer images must contain model dependencies, MLflow, FastAPI, Uvicorn, and
UID/GID `10001`. The service overrides their entrypoint, mounts the runner and
model artifacts read-only, and connects runtime containers to the internal
`ml-inference-runtime` network. Details are in
[the producer image runtime contract](model-runtime-base.md).

## Service release requirements

The API image is built from the service dependency base in
`projects/model-runtime-base`. `BASE_IMAGE` remains digest-pinned for repeatable
API builds. The production host requires Docker Engine, the Docker CLI in the
API container, access to `/var/run/docker.sock`, the shared runtime network,
and `/var/lib/ml-inference-service/model-artifacts`.

The release bundle no longer mounts `release.env` into the API as a runtime
image manifest. Runtime image selection comes from MLflow model-version
metadata, resolved against local Docker images at deployment time.
