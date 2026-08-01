import os

from api.scheduler import _dispatch_index, _index_sources


class TestIndexSources:
    def test_defaults_to_s3(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": ""})
        assert _index_sources() == ["tasks.index_albums"]

    def test_s3_source(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": "s3"})
        assert _index_sources() == ["tasks.index_albums"]

    def test_navidrome_source(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": "navidrome"})
        assert _index_sources() == ["tasks.index_navidrome"]

    def test_both_sources(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": "both"})
        assert _index_sources() == ["tasks.index_albums", "tasks.index_navidrome"]

    def test_unknown_source_falls_back_to_s3(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": "bogus"})
        assert _index_sources() == ["tasks.index_albums"]


class TestDispatchIndex:
    def test_dispatches_configured_tasks(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": "both"})
        dispatch = mocker.patch("api.scheduler._dispatch_task", return_value=True)

        result = _dispatch_index()

        assert result is True
        assert dispatch.call_args_list[0].args[0] == "tasks.index_albums"
        assert dispatch.call_args_list[1].args[0] == "tasks.index_navidrome"

    def test_navidrome_uses_dedicated_lock(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": "both"})
        dispatch = mocker.patch("api.scheduler._dispatch_task", return_value=True)

        _dispatch_index()

        s3_key = dispatch.call_args_list[0].args[1]
        nav_key = dispatch.call_args_list[1].args[1]
        assert s3_key != nav_key
        assert "navidrome" in nav_key

    def test_returns_false_when_nothing_dispatched(self, mocker):
        mocker.patch.dict(os.environ, {"INDEX_SOURCE": "navidrome"})
        mocker.patch("api.scheduler._dispatch_task", return_value=False)

        assert _dispatch_index() is False
