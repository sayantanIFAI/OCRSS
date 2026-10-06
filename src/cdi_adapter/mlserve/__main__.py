"""``python -m cdi_adapter.mlserve`` — the model gateway on localhost:``CDI_MLSERVE_PORT``."""

import uvicorn

from cdi_adapter import tracing
from cdi_adapter.config import get_settings
from cdi_adapter.logging import configure


def main() -> None:
    configure()
    tracing.setup_from_settings(tracing.MLSERVE)
    uvicorn.run("cdi_adapter.mlserve.app:app", host="127.0.0.1", port=get_settings().mlserve_port)


if __name__ == "__main__":
    main()
