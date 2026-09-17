# mykaggle

Kaggle CLI modeled on `google-colab-cli`: one connected session at a time,
kernel-backed `exec` / `repl`, Contents API uploads, `%pip` installs.

(Kaggle's proxy only routes `/api/*`, so there is no server-terminal
equivalent of `colab console` / `colab ssh` — everything goes over kernel
channels. See `test-muse` notes for the probe results.)

## Install

```bash
cd ~/personal/mykaggle
uv sync
# then either:
uv run mykaggle --help
# or install the binary on PATH:
uv tool install .
```

## Usage

```bash
# Connect (prints kernel info, saves session to ~/.config/mykaggle/session.json).
# Re-running replaces the previous session — one at a time.
mykaggle 'https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<jwt>/proxy'
mykaggle connect 'https://kkb-production.jupyter-proxy.kaggle.net?token=<jwt>'

# Execute inline code / a file (uploads, then %run) / stdin
mykaggle exec "import torch; print(torch.cuda.get_device_name(0))"
mykaggle exec -f train.py
echo "print(1 + 1)" | mykaggle exec

# Interactive REPL on the kernel
mykaggle repl

# Install packages on the kernel
mykaggle install requests httpx
mykaggle install -r requirements.txt
```

Global options: `--config PATH` (override state file), `--logtostderr`.

## Layout (mirrors colab-cli)

```
src/mykaggle/
  cli.py          typer app, AlphabeticalGroup, `mykaggle <url>` callback
  common.py       global state, resolve_session(), logging
  state.py        single SessionState + locked StateStore
  runtime.py      KaggleRuntime: kernel WS client, execute_code + hooks
  net.py          retries + friendly network errors (credentials masked)
  contents.py     ContentsClient: upload/download
  repl.py         prompt-toolkit REPL
  utils.py        output rendering (text, display data, images)
  urls.py         ?token= and /k/.../proxy URL handling
  commands/
    session.py    connect
    execution.py  exec, repl
    packages.py   install
```
