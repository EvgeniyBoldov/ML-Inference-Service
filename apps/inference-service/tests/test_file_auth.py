from hashlib import sha256

import pytest

from app.auth import FileTokenAuth
from app.errors import ServiceError


@pytest.mark.asyncio
async def test_file_tokens_limit_permissions_by_role(tmp_path) -> None:
    token = "predict-secret"
    token_file = tmp_path / "tokens"
    token_file.write_text(f"predict_1 predict {sha256(token.encode()).hexdigest()}\n")
    auth = FileTokenAuth(str(token_file))

    await auth.require("inference.predict")(authorization=f"Bearer {token}")
    with pytest.raises(ServiceError) as error:
        await auth.require("deployment.write")(authorization=f"Bearer {token}")

    assert error.value.status_code == 403
