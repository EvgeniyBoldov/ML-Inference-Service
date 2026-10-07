# DevOps: producer images and runtime environments

The Inference Service does not install model libraries and does not pull
producer images during deployment. Airflow and Jupyter images carry the Python
environment used to load their models.

## Producer image build contract

Each Airflow/Jupyter build should use a unique immutable tag and include:

- the model's Python and native dependencies;
- `mlflow`, `fastapi`, and `uvicorn`;
- a user/group with UID/GID `10001`.

Set this variable in the corresponding Airflow/Jupyter services:

```text
ML_INFERENCE_PRODUCER_IMAGE=registry.company.local/ml-airflow:2026.10.06-17
```

The Airflow scheduler and workers that publish models must use the same image
and value. The logging package writes the variable to model-version tag
`ml_inference.producer_image`. Never reuse a tag for different contents.

## Production rollout

Preload the exact producer image on the production Docker host before deploying
a model. Runtime startup uses `--pull=never`; absent images produce
`PRODUCER_IMAGE_UNAVAILABLE`. Keep old images while active groups or rollback
retention still use them. See [the runtime contract](model-runtime-base.md).

The API release still builds its own dependencies from
`projects/model-runtime-base/requirements.txt`; changing those affects the
control plane only. Changing model dependencies requires a new producer image,
updated producer variable, and redeployment of the model.
