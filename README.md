# mykaggle

Streamlined Kaggle CLI organized into three clean subcommand groups:
- **`mykaggle session`**: Manage Kaggle kernel entities and interactive VM sessions.
- **`mykaggle process`**: Manage code execution, background jobs, logs, and process lifecycles on active sessions.
- **`mykaggle uv`**: Build lightweight wheelhouses for Kaggle offline environments by filtering pre-installed packages.

## Install

```bash
cd ~/personal/mykaggle
uv sync

# Install global binary on PATH:
XDG_DATA_HOME=$HOME/.local/share XDG_BIN_HOME=$HOME/.local/bin uv tool install --force .
```

## Quick Reference

### 1. Session Management (`mykaggle session`)

```bash
# List your Kaggle kernels and view active connected session (JSON by default, --table for table)
mykaggle session ls
mykaggle session ls --table
mykaggle session ls -s batch -n 10

# Create a brand new kernel entity on Kaggle
mykaggle session create my-new-experiment -t "My New Experiment" -m l4 -c gemma-4-developer-agent --start
mykaggle session create my-script --type script -f train.py

# Start (or restart) an interactive Kaggle VM session and connect to it
# (auto-creates the kernel if it doesn't already exist on Kaggle)
mykaggle session start carlesoctav/mykaggle-batch-test -m l4       # 4x NVIDIA L4
mykaggle session start carlesoctav/mykaggle-batch-test -m t4 --internet
mykaggle session start -m p100                                     # reuses last kernel slug
mykaggle session start -m tpu
mykaggle session start -m cpu

# Cancel / shut down active Kaggle VM session
mykaggle session stop

# Delete a kernel from Kaggle
mykaggle session delete my-new-experiment -y

# Connect manually with proxy URL
mykaggle session connect 'https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<jwt>/proxy'
```

### 2. Process & Execution (`mykaggle process`)

```bash
# Run shell command on the main notebook kernel (streaming output + logging to .mykaggle/logs/main.log)
mykaggle process run "nvidia-smi"

# Run a dedicated named kernel / background job
mykaggle process run -n train -d "python train.py --epochs 10"

# Run a local Python file with arguments (streams stdout/stderr, injected into sys.argv)
mykaggle process run -f train.py -- --epochs 10 --batch-size 32
mykaggle process run -f train.py -n worker -d -- --epochs 50

# View or follow logs
mykaggle process logs                 # list all recorded kernels/jobs as JSON (--table for table)
mykaggle process logs --table         # list recorded kernels/jobs as human-readable table
mykaggle process logs train           # print last lines
mykaggle process logs train -f        # stream/follow output

# Interrupt a running kernel (SIGINT; defaults to 'main')
mykaggle process int
mykaggle process int train
mykaggle process int -n train

# Stop and delete a kernel process
mykaggle process rm train
mykaggle process rm -n train

# Dynamically attach or refresh datasets, models, kernel outputs, or competitions in /kaggle/input
mykaggle process attach owner/dataset-slug
mykaggle process attach owner/dataset-slug -v 3
mykaggle process attach google/gemma-2/pytorch/gemma-2-2b-it/2
mykaggle process attach owner/kernel-slug --kernel
mykaggle process attach gemma-4-developer-agent --competition

# Submit a batch "Save & Run All" commit for a kernel slug
mykaggle process commit carlesoctav/mykaggle-batch-test
mykaggle process commit carlesoctav/mykaggle-batch-test -f train.py -m l4 -w -o ./out
mykaggle process commit carlesoctav/mykaggle-batch-test -c "python train.py --epochs 10" -m t4 -w
```

### 3. UV & Wheelhouse Management (`mykaggle uv`)

```bash
# Preview packages that would be built without downloading/building (dry run)
mykaggle uv make-wheel --dry-run
mykaggle uv make-wheel --dry-run /path/to/project

# Build wheels for project dependencies into ./kaggle_wheelhouse (filters Kaggle pre-installed packages)
mykaggle uv make-wheel

# Specify project directory or custom wheelhouse output directory
mykaggle uv make-wheel /path/to/project -o ./my_wheelhouse
mykaggle uv make-wheel ./dist_wheels

# Build all dependencies without filtering Kaggle pre-installed packages
mykaggle uv make-wheel --full

# Strict constrained resolution against exact Kaggle version pins (resolves version conflicts automatically)
mykaggle uv make-wheel-strict --dry-run /path/to/project
mykaggle uv make-wheel-strict /path/to/project -o ./my_wheelhouse
mykaggle uv make-wheel-strict --full
```

## Global Options

- `--config PATH`: Path to session state file (defaults to `~/.config/mykaggle/session.json`)
- `--logtostderr`: Log all debug output to stderr
- `-h, --help`: Show help for any command or group
