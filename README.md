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
# Create a brand new kernel on Kaggle (with optional initial accelerator / data sources)
mykaggle new my-new-experiment -t "My New Experiment" -m l4 -c gemma-4-developer-agent --start
mykaggle new my-script --type script -f train.py

# Delete a kernel from Kaggle
mykaggle delete-kernel my-new-experiment -y

# Start (or restart) an interactive Kaggle VM session and auto-connect (auto-creates kernel if it does not exist)
mykaggle start carlesoctav/mykaggle-batch-test -m l4       # 4x NVIDIA L4 (on supported competition kernels)
mykaggle start carlesoctav/mykaggle-batch-test -m t4 --internet
mykaggle start -m p100                                     # reuses last kernel_slug
mykaggle start -m tpu
mykaggle start -m cpu
mykaggle cancel                                            # shut down active Kaggle VM session

# Trigger a batch "Save & Run All" commit for a kernel slug (preserves datasets/models/settings)
mykaggle commit carlesoctav/mykaggle-batch-test
mykaggle commit carlesoctav/mykaggle-batch-test -f train.py -m l4 -w -o ./out
mykaggle commit carlesoctav/mykaggle-batch-test -c "python train.py --epochs 10" -m t4 -w

# Or connect manually with a Jupyter proxy URL:
mykaggle 'https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<jwt>/proxy'
mykaggle connect 'https://kkb-production.jupyter-proxy.kaggle.net?token=<jwt>'

# Execute inline code / a file (uploads, then %run) / stdin
mykaggle exec "import torch; print(torch.cuda.get_device_name(0))"
mykaggle exec -f train.py
echo "print(1 + 1)" | mykaggle exec

# Run shell commands or local Python files on named kernels (reused if name exists)
mykaggle run "nvidia-smi"
mykaggle run -n train -d "python train.py --epochs 10"
mykaggle run-file train.py -n train -d -- --epochs 10

# View/follow logs & manage named kernels
mykaggle logs
mykaggle logs train -f
mykaggle int train
mykaggle restart train
mykaggle stop train   # or: mykaggle rm train

# Dynamically attach or refresh datasets, models, kernel outputs, or competitions in /kaggle/input
mykaggle attach owner/dataset-slug
mykaggle attach owner/dataset-slug -v 3
mykaggle attach google/gemma-2/pytorch/gemma-2-2b-it/2
mykaggle attach owner/kernel-slug --kernel
mykaggle attach gemma-4-developer-agent --competition

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
