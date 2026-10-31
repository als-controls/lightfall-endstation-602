import pytest
from tiled.catalog import in_memory
from tiled.client import Context, from_context
from tiled.server.app import build_app


@pytest.fixture(scope="session")
def tiled_app(tmp_path_factory):
    # build_app takes ~9 s here, so one in-memory catalog serves the whole session;
    # tests that write runs must use unique uids.
    tmp = tmp_path_factory.mktemp("tiled")
    sql_uri = f"sqlite:///{(tmp / 'internal.db').as_posix()}"
    return build_app(in_memory(writable_storage=[str(tmp), sql_uri]))


@pytest.fixture
def tiled_client(tiled_app):
    with Context.from_app(tiled_app) as context:
        yield from_context(context)
