from __future__ import annotations

from gevent import monkey

monkey.patch_all()

import atexit
from functools import partial
import sys
import threading
from datetime import datetime
import json
import os
from pathlib import Path
import pty
import select
import fcntl
import shlex
import struct
import termios
import time
import signal
from typing import TYPE_CHECKING, List
from subprocess import Popen

import yaml
import typer
from loguru import logger
from schema import Optional, Or, Schema, SchemaError
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from flask_socketio import SocketIO
from flask_login import LoginManager, login_user, logout_user, current_user

from . import __version__

if TYPE_CHECKING:
    from geventwebsocket import WebSocketServer

logger.disable("websole")

cli = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)
app = Flask(__name__, static_folder="templates/assets")
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

app.config["lock"] = threading.Lock()
app.config["fd"] = None
app.config["proc"] = None
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


def exit_handler():
    proc = app.config["proc"]
    if proc:
        kill_proc(proc)


@app.route("/")
def index():
    return redirect(url_for("console"))


def get_template_kws():
    return {
        "brand": app.config["brand"],
        "icons": app.config["icons"],
        "links": app.config["links"],
        "year": datetime.now().year,
        "allowRestart": app.config["allow_restart"],
        "useShortcut": app.config["use_shortcut"],
        "hideUseShortcutSwitch": app.config["hide_use_shortcut_switch"],
        "whatIsWebpassUrl": app.config["what_is_webpass_url"],
        "hasPassword": bool(app.config["webpass"]),
    }


def is_authenticated():
    webpass = app.config.get("webpass", None)
    if (not webpass) or current_user.is_authenticated:
        return True
    else:
        return False


@app.route("/console")
def console():
    if not is_authenticated():
        return login_manager.unauthorized()
    else:
        return render_template("console.html", **get_template_kws())


@app.route("/login", methods=["GET"])
def login():
    webpass = app.config.get("webpass", None)
    if webpass is not None and not webpass:
        login_user(DummyUser())
        return redirect(request.args.get("next") or url_for("index"))
    else:
        return render_template("login.html", **get_template_kws())


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


@app.route("/logout")
def logout():
    logout_user()
    return redirect("/login")


@app.route("/heartbeat")
def heartbeat():
    webpass = app.config.get("webpass", None)
    webpass_input = request.args.get("p", None)
    if (webpass_input is None) or (webpass is None):
        return abort(403)
    if (not webpass) or (webpass_input == webpass):
        if app.config["proc"] is None:
            start_proc()
            return jsonify({"status": "restarted", "pid": app.config["proc"].pid}), 201
        else:
            return jsonify({"status": "running", "pid": app.config["proc"].pid}), 200
    else:
        return abort(403)


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html", **get_template_kws()), 404


@socketio.on("pty-input", namespace="/pty")
def pty_input(data):
    if not is_authenticated():
        return
    with app.config["lock"]:
        if app.config["fd"]:
            i = data["input"].encode()
            os.write(app.config["fd"], i)


def set_size(fd, row, col, xpix=0, ypix=0):
    logger.debug(f"Resizing pty to: {row} {col}.")
    size = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


@socketio.on("resize", namespace="/pty")
def resize(data):
    logger.debug("Received resize socketio signal.")
    if not is_authenticated():
        return
    with app.config["lock"]:
        if app.config["fd"]:
            set_size(app.config["fd"], data["rows"], data["cols"])


def read_and_forward_pty_output():
    max_read_bytes = 1024 * 20
    while True:
        if app.config["fd"]:
            (data, _, _) = select.select([app.config["fd"]], [], [])
            if data:
                with app.config["lock"]:
                    if app.config["fd"]:
                        output = os.read(app.config["fd"], max_read_bytes).decode(errors="ignore")
                        app.config["hist"] += output
                        socketio.emit("pty-output", {"output": output}, namespace="/pty")
                    else:
                        break
        else:
            break


def disconnect_on_proc_exit(proc: Popen):
    returncode = proc.wait()
    if proc == app.config["proc"]:
        logger.debug(f"Command exited with return code {returncode}.")
        output = (
            f"\r\n\nThe program has exited with returncode {returncode}. "
            "\r\nRefresh the page to restart the program."
        )
        app.config["hist"] += output
        socketio.emit("pty-output", {"output": output}, namespace="/pty")


def start_proc():
    master_fd, slave_fd = pty.openpty()
    p = Popen(app.config["command"], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, preexec_fn=os.setsid)
    socketio.start_background_task(target=disconnect_on_proc_exit, proc=p)
    atexit.register(exit_handler)
    app.config["fd"] = master_fd
    app.config["proc"] = p
    logger.debug(f"Commond started at: {p.pid} ({truncate_str(shlex.join(app.config['command']), 20)}).")
    socketio.start_background_task(target=read_and_forward_pty_output)


@socketio.on("cmd_run", namespace="/pty")
def run(data):
    logger.debug("Received cmd_run socketio signal.")
    if not is_authenticated():
        return
    with app.config["lock"]:
        if app.config["fd"] and app.config["proc"] and app.config["proc"].poll() is None:
            set_size(app.config["fd"], data["rows"], data["cols"])
            socketio.emit("pty-output", {"output": app.config["hist"]}, namespace="/pty")
        else:
            start_proc()
            set_size(app.config["fd"], data["rows"], data["cols"])


def kill_proc(proc: Popen):
    proc.send_signal(signal.SIGINT)
    for _ in range(10):
        poll = proc.poll()
        if poll is not None:
            break
    else:
        proc.kill()
    logger.debug(f"Process killed: {proc.pid}.")


@socketio.on("cmd_stop", namespace="/pty")
def stop():
    logger.debug("Received cmd_stop socketio signal.")
    if not is_authenticated():
        return
    if not app.config["allow_restart"]:
        return
    with app.config["lock"]:
        proc = app.config["proc"]
        if proc is not None:
            app.config["fd"] = None
            app.config["proc"] = None
            app.config["hist"] = ""
    if proc is not None:
        socketio.start_background_task(target=kill_proc, proc=proc)


def check_config(config: dict):
    schema = Schema(
        {
            Optional("command"): Or(str, List[str]),
            Optional("host"): str,
            Optional("port"): int,
            Optional("webpass"): Or(str, int),
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
            Optional("allow_restart"): bool,
            Optional("use_shortcut"): bool,
            Optional("hide_use_shortcut_switch"): bool,
            Optional("what_is_webpass_url"): str,
            Optional("start"): bool,
        }
    )
    try:
        schema.validate(config)
    except SchemaError as e:
        return e
    else:
        return None


def configure(dry=False, **kw):
    default_config = {
        "command": None,
        "host": "localhost",
        "port": 1818,
        "webpass": None,
        "brand": "",
        "icons": [],
        "links": [],
        "allow_restart": True,
        "use_shortcut": False,
        "hide_use_shortcut_switch": False,
        "what_is_webpass_url": "",
        "start": False,
    }
    for k, v in default_config.items():
        kw.setdefault(k, v)
    if not kw["links"]:
        kw["links"].append({"label": "Powered by Websole", "url": "https://github.com/jackzzs/websole/"})
    if dry:
        print(json.dumps(kw))
        exit(0)
    else:
        app.config.update(kw)

def terminate(signal, frame, server: WebSocketServer):
  logger.info("Server shutting down due to termination signal.")
  server.stop()
  sys.exit(0)

def serve():
    from geventwebsocket import WebSocketServer

    host = app.config.get("host", "localhost")
    port = app.config.get("port", 1818)

    if host == "0.0.0.0" and not os.environ.get("_WEB_ISOLATED", None):
        logger.warning(
            f"Web console host set to 0.0.0.0 and will listen on all interfaces, please pay attention to security issues."
        )

    server = WebSocketServer((host, port), app)
    logger.info(f"Web console started at {host}:{port}.")
    
    try:
        signal.signal(signal.SIGTERM, partial(terminate, server=server))
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down due to keyboard signal.")
        server.stop()
        sys.exit(0)
    except Exception as e:
        time.sleep(3)
        logger.info(f"Server shutting down due to unknown error: {e}")
        sys.exit(1)

class TyperCommand(typer.core.TyperCommand):
    def get_usage(self, ctx) -> str:
        return super().get_usage(ctx) + " COMMAND"


def version(version):
    if version:
        print(__version__)
        raise typer.Exit()


@cli.command(cls=TyperCommand, context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def main(
    ctx: typer.Context,
    host: str = typer.Option(
        None,
        "--host",
        envvar="_WEB_HOST",
        show_envvar=False,
        show_default="locaalhost",
        help="Host for web console to listen on.",
    ),
    port: int = typer.Option(
        None,
        "--port",
        envvar="_WEB_PORT",
        show_envvar=False,
        show_default=1818,
        help="Port for web console to listen on.",
    ),
    webpass: str = typer.Option(
        None,
        "--webpass",
        envvar="_WEB_PASS",
        show_envvar=False,
        help="Password for login to web console.",
    ),
    brand: str = typer.Option(
        None,
        "--brand",
        envvar="_WEB_BRAND",
        show_envvar=False,
        help="Brand to be shown in web console header.",
    ),
    icons: List[str] = typer.Option([], "--icon", "-i", help="Icons to be shown in web console footer."),
    links: List[str] = typer.Option([], "--link", "-l", help="Links to be shown in web console footer."),
    config: Path = typer.Option(
        "config.yml",
        "--config",
        envvar="_WEB_CONFIG",
        show_envvar=False,
        help="Location of the config file.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        envvar="_WEB_DEBUG",
        show_envvar=False,
        help="Serve web console in debug mode.",
    ),
    dry: bool = typer.Option(
        False,
        "--dry",
        help="Show config only, do not start web console.",
    ),
    start: bool = typer.Option(
        True,
        "--start/--no-start",
        help="Start the program on start.",
    ),
    version: bool = typer.Option(
        None,
        "--version",
        callback=version,
        is_eager=True,
        help="Print version and exit.",
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
        if len(ctx.args) == 1:
            config["command"] = shlex.split(ctx.args[0])
        else:
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
    else:
        webpass = config.get("webpass", None)
        if webpass:
            config["webpass"] = str(webpass)
    if start:
        config["start"] = start
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
    configure(debug=debug, dry=dry, **config)
    if config["start"]:
        start_proc()
    serve()


if __name__ == "__main__":
    cli()
