import uvicorn

from . import config
from .api.app import create_app

HOST = "127.0.0.1"  # loopback only, by design
DEFAULT_PORT = config.DEFAULT_PORT


def port() -> int:
    return config.port()


if __name__ == "__main__":
    uvicorn.run(create_app(), host=HOST, port=port(), log_level="info")
