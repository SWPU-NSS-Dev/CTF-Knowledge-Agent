import json

from typer.testing import CliRunner

from ctfka.cli.main import _settings, app


def test_init_writes_config_and_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["token"]
    assert (tmp_path / "config.local.yaml").exists()


def test_global_data_dir_search_outputs_json(tmp_path):
    # Store initialization and empty search are real local operations.
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "search", "shellcode"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"items": []}


def test_config_local_takes_precedence_without_global_option(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("data_dir: ./template-data\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text("data_dir: ./local-data\n", encoding="utf-8")
    assert _settings(None, None).data_dir.resolve() == (tmp_path / "local-data").resolve()


def test_serve_passes_selected_settings_to_uvicorn(tmp_path, monkeypatch):
    import uvicorn

    config = tmp_path / "custom.yaml"
    config.write_text(
        f"data_dir: {str(tmp_path / 'service-data').replace(chr(92), '/')}\nhost: 127.0.0.2\nport: 8123\ntoken: secret\n",
        encoding="utf-8",
    )
    captured = {}

    def capture_run(application, **kwargs):
        captured.update(application=application, **kwargs)

    monkeypatch.setattr(uvicorn, "run", capture_run)
    result = CliRunner().invoke(app, ["--config", str(config), "serve"])
    assert result.exit_code == 0, result.output
    assert captured["application"].state.settings.data_dir == (tmp_path / "service-data").resolve()
    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 8123


def test_worker_command_is_available():
    result = CliRunner().invoke(app, ["worker", "--help"])
    assert result.exit_code == 0, result.output
    assert "Continuously process queued" in result.output


def test_crawl_exposes_discovery_and_processing_limits():
    result = CliRunner().invoke(app, ["crawl", "--help"])
    assert result.exit_code == 0, result.output
    assert "--max-items" in result.output
    assert "--max-jobs" in result.output


def test_crawl_exits_nonzero_when_fetches_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.local.yaml").write_text(
        "data_dir: ./data\nsources:\n  blocked:\n    adapter: html\n    max_items: null\n    options:\n      start_urls:\n        - http://127.0.0.1/private\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["crawl", "--source", "blocked", "--max-items", "1", "--max-jobs", "1"],
    )
    assert result.exit_code == 1, result.output
    output = json.loads(result.output)
    assert output["run"]["failed"] == 1
    assert output["run"]["completed"] == 0


def test_demo_seed_and_graph_attack_fix_are_explicit_json_workflow(tmp_path):
    runner = CliRunner()
    seeded = runner.invoke(app, ["--data-dir", str(tmp_path), "demo", "seed-awdp"])
    assert seeded.exit_code == 0, seeded.output
    fixture = json.loads(seeded.output)
    assert fixture["fixture"] is True
    compared = runner.invoke(app, [
        "--data-dir", str(tmp_path), "graph", "attack-fix",
        "--entity-id", fixture["entities"]["challenge"],
    ])
    assert compared.exit_code == 0, compared.output
    assert len(json.loads(compared.output)["comparisons"]) >= 2


def test_review_submit_missing_target_exits_nonzero(tmp_path):
    result = CliRunner().invoke(app, [
        "--data-dir", str(tmp_path), "review", "submit",
        "--target-type", "entity", "--target-id", "missing-record",
        "--decision", "rejected", "--note", "not found regression",
        "--reviewer", "test", "--expected-revision", "1",
    ])
    assert result.exit_code == 1
    assert "target not found" in result.output
    assert result.output.strip() != "null"
