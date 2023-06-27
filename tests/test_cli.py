import json
import shlex
from typer.testing import CliRunner

import websole
from websole.app import cli

runner = CliRunner()


def test_version():
    result = runner.invoke(cli, ["--version"])
    assert websole.__version__ in result.stdout
    assert result.exit_code == 0


def test_options():
    cmd = "--dry -h 0.0.0.0 -p 80 -w '' -i bi-0-circle-fill:http://localhost -l Link:http://localhost echo 'Hello Websole'"
    result = runner.invoke(cli, shlex.split(cmd))
    assert result.exit_code == 0
    result = json.loads(result.stdout)
    assert isinstance(result["command"], list)
    assert "echo" in result["command"]
    assert isinstance(result["port"], int)
    assert isinstance(result["icons"], list)
    assert isinstance(result["links"], list)
