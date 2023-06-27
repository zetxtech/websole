import os
from pathlib import Path
import pty
import select
import fcntl
import shlex
import struct
import subprocess
import termios
import time
import signal
from typing import List

import yaml
import typer
from loguru import logger
from schema import Optional, Or, Schema, SchemaError
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from flask_socketio import SocketIO
from flask_login import LoginManager, login_user, login_required, current_user

logger.disable("websole")

cli = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)
app = Flask(__name__, static_folder="templates/assets")
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

app.config["fd"] = None
app.config["pid"] = None
app.config["hist"] = ""
app.config["faillog"] = []


def truncate_str(text: str, length: int):
    return f"{text[:length + 3]}..." if len(text) > length else text


class DummyUser:
    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return 0


@login_manager.user_loader
def load_user(_):
    return DummyUser()


@app.route("/")
def index():
    return redirect(url_for("console"))


@app.route("/console")
@login_required
def console():
    return render_template(
        "console.html", brand=app.config["brand"], icons=app.config["icons"], links=app.config["links"]
    )


@app.route("/login", methods=["GET"])
def login():
    return render_template(
        "login.html", brand=app.config["brand"], icons=app.config["icons"], links=app.config["links"]
    )


@app.route("/login", methods=["POST"])
def login_submit():
    webpass = app.config.get("webpass", None)
    webpass_input = request.form.get("webpass", "")
    if webpass is None:
        emsg = "Web console password not set."
    elif sum(t > time.time() - 3600 for t in app.config["faillog"][-5:]) == 5:
        emsg = "Too many login trials in one hour."
    else:
        if (not webpass) or (webpass_input == webpass):
            login_user(DummyUser())
            return redirect(request.args.get("next") or url_for("index"))
        else:
            emsg = "Wrong password."
            app.config["faillog"].append(time.time())
    return render_template("login.html", emsg=emsg)


@app.route("/healthz")
def healthz():
    return "200 OK"


@app.route("/heartbeat")
def heartbeat():
    webpass = app.config.get("webpass", None)
    webpass_input = request.args.get("p", None)
    if (webpass_input is None) or (webpass is None):
        return abort(403)
    if (not webpass) or (webpass_input == webpass):
        if app.config["pid"] is None:
            (pid, fd) = pty.fork()
            if pid == 0:
                subprocess.run(app.config["command"])
            else:
                app.config["fd"] = fd
                app.config["pid"] = pid
                logger.debug(
                    f"Commond started at: {pid} ({truncate_str(shlex.join(app.config['command']), 20)})."
                )
            return jsonify({"status": "restarted", "pid": pid}), 201
        else:
            return jsonify({"status": "running", "pid": app.config["pid"]}), 200
    else:
        return abort(403)


@app.errorhandler(404)
def page_not_found(e):
    return (
        render_template(
            "404.html", brand=app.config["brand"], icons=app.config["icons"], links=app.config["links"]
        ),
        404,
    )


@socketio.on("pty-input", namespace="/pty")
def pty_input(data):
    if not current_user.is_authenticated():
        return
    if app.config["fd"]:
        os.write(app.config["fd"], data["input"].encode())


def set_size(fd, row, col, xpix=0, ypix=0):
    logger.debug(f"Resizing pty to: {row} {col}.")
    size = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


@socketio.on("resize", namespace="/pty")
def resize(data):
    if not current_user.is_authenticated():
        return
    if app.config["fd"]:
        set_size(app.config["fd"], data["rows"], data["cols"])


def read_and_forward_pty_output():
    max_read_bytes = 1024 * 20
    while True:
        socketio.sleep(0.01)
        if app.config["fd"]:
            (data, _, _) = select.select([app.config["fd"]], [], [], 0)
            if data:
                output = os.read(app.config["fd"], max_read_bytes).decode(errors="ignore")
                app.config["hist"] += output
                socketio.emit("pty-output", {"output": output}, namespace="/pty")


@socketio.on("cmd_run", namespace="/pty")
def run(data):
    if not current_user.is_authenticated():
        return
    if app.config["fd"]:
        set_size(app.config["fd"], data["rows"], data["cols"])
        socketio.emit("pty-output", {"output": app.config["hist"]}, namespace="/pty")
    else:
        (pid, fd) = pty.fork()
        if pid == 0:
            subprocess.run([*app.config["command"]])
        else:
            app.config["fd"] = fd
            app.config["pid"] = pid
            logger.debug(
                f"Commond started at: {pid} ({truncate_str(shlex.join(app.config['command']), 20)})."
            )
            set_size(app.config["fd"], data["rows"], data["cols"])
            socketio.start_background_task(target=read_and_forward_pty_output)


@socketio.on("cmd_kill", namespace="/pty")
def stop():
    if not current_user.is_authenticated():
        return
    if app.config["pid"] is not None:
        os.kill(app.config["pid"], signal.SIGINT)
        for _ in range(50):
            try:
                os.kill(app.config["pid"], 0)
            except OSError:
                break
            else:
                time.sleep(0.1)
        else:
            os.kill(app.config["pid"], signal.SIGKILL)
        logger.debug(f"Command stopped: {app.config['pid']}.")
        app.config["fd"] = None
        app.config["pid"] = None
        app.config["hist"] = ""


def check_config(config: dict):
    schema = Schema(
        {
            Optional("command"): Or(str, List[str]),
            Optional("host"): str,
            Optional("port"): int,
            Optional("webpass"): Or(str, False),
            Optional("brand"): str,
            Optional("icons"): [
                Schema(
                    {
                        "icon": str,
                        "url": str,
                    }
                )
            ],
            Optional("links"): [
                Schema(
                    {
                        "label": str,
                        "url": str,
                    }
                )
            ],
        }
    )
    try:
        schema.validate(config)
    except SchemaError as e:
        return e
    else:
        return None


def serve(debug=False, **kw):
    default_config = {
        "command": None,
        "host": "localhost",
        "port": 1818,
        "webpass": None,
        "brand": "",
        "icons": [],
        "links": [],
    }
    app.config.update(kw)

    for k, v in default_config.items():
        app.config.setdefault(k, v)

    if not app.config["links"]:
        app.config["links"].append(
            {"label": "Powered by Websole", "url": "https://github.com/jackzzs/websole/"}
        )

    host = app.config.get("host", "localhost")
    port = app.config.get("port", 1818)

    logger.info(f"Web console started at {host}:{port}.")
    socketio.run(app, port=port, host=host, debug=debug)


class TyperCommand(typer.core.TyperCommand):
    def get_usage(self, ctx) -> str:
        return super().get_usage(ctx) + " COMMAND"


@cli.command(cls=TyperCommand, context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def main(
    ctx: typer.Context,
    host: str = typer.Option(
        None,
        "--host",
        "-h",
        envvar="_WEB_HOST",
        show_envvar=False,
        show_default="locaalhost",
        help="Host for web console to listen on.",
    ),
    port: int = typer.Option(
        None,
        "--port",
        "-p",
        envvar="_WEB_PORT",
        show_envvar=False,
        show_default=1818,
        help="Port for web console to listen on.",
    ),
    webpass: str = typer.Option(
        None,
        "--webpass",
        "-w",
        envvar="_WEB_PASS",
        show_envvar=False,
        help="Password for login to web console.",
    ),
    brand: str = typer.Option(
        None,
        "--brand",
        "-b",
        envvar="_WEB_BRAND",
        show_envvar=False,
        help="Brand to be shown in web console header.",
    ),
    icons: List[str] = typer.Option([], "--icon", "-i", help="Icons to be shown in web console footer."),
    links: List[str] = typer.Option([], "--link", "-l", help="Links to be shown in web console footer."),
    config: Path = typer.Option(
        None,
        "--config",
        "-c",
        envvar="_WEB_CONFIG",
        show_envvar=False,
        show_default="websole.yml",
        help="Location of the config file.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        "-d",
        envvar="_WEB_DEBUG",
        show_envvar=False,
        help="Serve web console in debug mode.",
    ),
):
    """
    Websole is a tool to expose command-line tools through web-based console.
    See: https://github.com/jackzzs/websole
    """

    logger.enable("websole")

    if config and Path(config).is_file():
        with open(config) as f:
            config = yaml.safe_load(f)
            err = check_config(config)
            if err:
                logger.opt(exception=err).error("Can not validate config file:")
                exit(1)
            else:
                if isinstance(config.get("command", None), str):
                    config["command"] = shlex.split(config["command"])
    else:
        config = {}
    if ctx.args:
        config["command"] = ctx.args
    else:
        env_command = os.environ.get("_WEB_COMMAND", None)
        if env_command:
            config["command"] = shlex.split(env_command)
    if not config.get("command", None):
        logger.error("Command should be specified.")
        exit(1)
    if host:
        config["host"] = host
    if port:
        config["port"] = port
    if webpass is not None:
        config["webpass"] = webpass
    if brand:
        config["brand"] = brand
    try:
        if icons:
            config["icons"] = [{"icon": i.split(":", 1)[0], "url": i.split(":", 1)[1]} for i in icons]
        if links:
            config["links"] = [{"label": l.split(":", 1)[0], "url": l.split(":", 1)[1]} for l in links]
    except IndexError:
        logger.error("Error during parsing icons and links, please check syntax.")
        exit(1)

    serve(debug=debug, **config)


if __name__ == "__main__":
    cli()
