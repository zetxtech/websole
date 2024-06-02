# Websole

Websole is a web-based console for you to expose command-line tools to a web server.

## Features

1. Protect your terminal with a login password.
2. Customizable logos, links, and icons.
3. Simple to use as a base docker image in other projects.

## Screenshot

<img src="https://github.com/jackzzs/websole/raw/main/images/example_console.png" alt="console page" width="600"/>

<img src="https://github.com/jackzzs/websole/raw/main/images/example_login.png" alt="login page" width="600"/>

## Install

You can expose any program (for example, `bash`) by running:

```bash
docker run -p 80:1818 --rm -it jackzzs/websole:main bash
```

This will expose a terminal running `bash` on port 80.

# Use in other projects

You can use websole as a base docker image, to expose your program.

See the `example` directory for examples.

# Commandline options

```
Usage: websole [OPTIONS] COMMAND

    --host              Host for web console to listen on (default: 127.0.0.1).
    --port              Port for web console to listen on (default: 1818).
    --webpass           Password for logging into the web console (default: <no password>).
    --brand             Brand to be shown in web console header (default: Web Console).
    --icon              Add an icon to be shown in web console footer (format: <icon>:<url>).
    --link              Add a link to be shown in web console footer (format: <label>:<url>).
    --config            Location of the config file (default: websole.yml).
    --start/--no-start  Whether to start the program immediately, rather than on first connection. (default: False)
    --version           Print version and exit.
    --help              Show help message and exit.

NOTE: If COMMAND contains spaces, you need to enclose it in quotes.
```

# Configuration file

Default config:
```yaml
# Program to be exposed to web console.
command: bash

# Host for web console to listen on.
host: 0.0.0.0

# Port for web console to listen on.
port: 1818

# Brand to be shown in web console header.
brand: Your Program

# Link to be shown in web console footer.
links:
  - label: Github
    url: https://github.com/jackzzs/websole
  - label: Example
    url: https://websole.onrender.com

# Icons to be shown in web console footer.
# icon names can be found from https://icons.getbootstrap.com/
icons:
  - icon: bi-github
    url: https://github.com/jackzzs/websole
  - icon: bi-fire
    url: https://websole.onrender.com

# Password for logging into the web console.
webpass: 123456

# Whether to start the program immediately, rather than on first connection.
start: yes

# URL for "What is web console password?" link on the login page.
what_is_webpass_url: https://github.com/jackzzs/websole

# Allow users to restart the program through the refresh button in the upper right corner of the console.
allow_restart: yes

# By default, allow users to use Ctrl+C/V to copy/paste (but prevent the shortcut key from reaching the program).
use_shortcut: no

# Hide the switch for "use Ctrl+C/V to copy/paste".
hide_use_shortcut_switch: no
```
