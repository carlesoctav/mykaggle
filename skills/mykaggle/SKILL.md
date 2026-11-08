---
name: mykaggle
description: Operate Kaggle GPU/TPU VM sessions and offline competition workflows via `mykaggle` (`session` & `process`) and the `kaggle` CLI. Use when asked to create/start/stop Kaggle sessions, attach datasets/wheels/models, interactively debug scripts, track `kaggle/` experiments, or run batch commits.
---

# mykaggle

Use this skill whenever working with Kaggle sessions, offline competition environments (e.g., 4x NVIDIA L4), wheelhouses, or experiment tracking in `kaggle/`.

(Agents: this text is also printed by `mykaggle skill`.)

---

## 1. Division of Responsibilities: `mykaggle` vs `kaggle` CLI

- **`mykaggle` (`session`, `process`, & `uv`)**: Manages interactive Kaggle VM lifecycle, inside-VM execution over the Jupyter proxy, and offline wheelhouse building:
  - `mykaggle session {create, start, stop, delete, ls, connect}` — provision, start/restart, list, connect to, or shut down interactive Kaggle VMs.
  - `mykaggle process {run, attach, logs, int, rm, commit}` — run commands/scripts on named Jupyter kernels, dynamically mount/refresh `/kaggle/input` datasources, stream logs, and trigger batch commits.
  - `mykaggle uv {make-wheel, make-wheel-strict} [path] [--dry-run] [--full]` — export or compile project dependencies and build a lightweight wheelhouse filtering pre-installed Kaggle packages.
- **`kaggle` CLI (for operations outside kernels)**: All dataset creation/versioning, model management, competition submissions, and batch output downloads happen outside kernels via the standard `kaggle` CLI:
  - `kaggle datasets create -p <dir> --dir-mode skip`
  - `kaggle datasets version -p <dir> -m "notes" --dir-mode skip`
  - `kaggle datasets list -m` / `kaggle datasets status <owner/slug>`
  - `kaggle kernels status <owner/slug>` / `kaggle kernels output <owner/slug> -p ./output`

---

## 2. Kaggle Environment & Constraints

When running or training in an offline competition container (`enable_internet: false`):

| Path / Feature | Status | Behavior / Rules |
| :--- | :--- | :--- |
| **Internet Access** | ❌ **Disabled** | External DNS and sockets fail (`pip install` from PyPI, HF Hub downloads, and external APIs fail). **Exception**: Kaggle's internal Data Proxy (`dp.kaggle.net`) remains reachable inside the VM, which `mykaggle process attach` uses. |
| **`/kaggle/input`** | 🔒 **Read-Only (`ro`)** | Mounted via host-level FUSE/NFS. Cannot be modified directly inside the VM, **but** you can dynamically mount new datasets or refresh updated dataset versions live via `mykaggle process attach`. |
| **`/kaggle/working`** | ✏️ **Read-Write (`rw`)** | Temporary ext4 workspace disk. All checkpoints, logs, and outputs must be written here. |

---

## 3. Interactive Development with `mykaggle`

`mykaggle` is split into two subcommands: `mykaggle session` and `mykaggle process`. Session state is stored in `~/.config/mykaggle/session.json`.

### A. Manage the Kaggle VM (`mykaggle session`)

```bash
# List your Kaggle kernels and see the currently connected session (JSON by default; --table for table)
mykaggle session ls
mykaggle session ls --table

# Create a brand new kernel on Kaggle (and optionally --start the VM immediately)
mykaggle session create my-exp -t "My Experiment" -m l4 -c gemma-4-developer-agent --start

# Start (or restart) an interactive VM session and auto-connect
# (Automatically cancels the previous VM session first unless --keep-previous is passed)
mykaggle session start carlesoctav/my-exp -m l4
mykaggle session start carlesoctav/my-exp -m t4 --internet

# Shut down the active Kaggle VM session (saves GPU quota!)
mykaggle session stop

# Delete a kernel entity from Kaggle
mykaggle session delete carlesoctav/my-exp -y

# Or connect manually by pasting a Jupyter proxy URL:
mykaggle session connect 'https://kkb-production.jupyter-proxy.kaggle.net/k/<id>/<jwt>/proxy'
```

### B. Run Code & Manage Processes (`mykaggle process`)

Requires an active session from `mykaggle session start` (or `mykaggle session connect`).

```bash
# Run a shell command on the default ('main') kernel
mykaggle process run "nvidia-smi"

# Run on a dedicated named kernel ('scratch'), reusing it across calls
mykaggle process run -n scratch "python -c 'import torch; print(torch.cuda.device_count())'"

# Run a local Python script directly on a named kernel (without manual upload)
# Use -d / --detach to run in the background, and pass script args after --
mykaggle process run -n train -f kaggle/01_baseline.py -d -- --epochs 3 --lr 5e-5

# List all jobs/kernels as JSON (--table for Rich table), or stream logs of a specific kernel
mykaggle process logs
mykaggle process logs --table
mykaggle process logs -n train -f

# Send SIGINT (interrupt) or kill/remove a Jupyter kernel process
mykaggle process int -n train
mykaggle process rm -n train
```

---

## 4. Pre-Installed Environment & Offline Wheelhouse Workflow

### A. What Is Already Pre-Installed on the Kaggle VM
Kaggle's GPU image runs **Python 3.13** (`linux x86_64`, CUDA 13.0) with `uv` pre-installed and `UV_SYSTEM_PYTHON=true` set by default:
- **Already pre-installed (DO NOT bundle into your wheelhouse)**:
  - **PyTorch / CUDA**: `torch` (`2.11.0+cu130`), `triton` (`3.6.0`), `einops`, `timm`
  - **Hugging Face**: `transformers` (`5.20.2`), `accelerate` (`1.15.0`), `peft` (`0.21.2`), `datasets` (`4.8.5`), `tokenizers`, `safetensors`, `huggingface-hub`, `sentencepiece`
  - **JAX / TF / Keras**: `jax` (`0.11.1`), `flax`, `optax`, `tensorflow`, `keras`, `keras-hub`
  - **Data / Infra**: `numpy`, `scipy`, `pandas`, `scikit-learn`, `ray`, `wandb`, `pydantic`, `fastapi`
- **NOT pre-installed (bundle only if needed)**: `trl`, `bitsandbytes`, `unsloth`, `vllm`, `deepspeed`, `flash-attn`, `trackio`, plus your own local repo package (`src/`).
- If unsure whether a package is pre-installed, check inside the active VM first:
  ```bash
  mykaggle process run "uv pip list | grep -i <pkg>"
  ```

### B. Build the Lightweight Wheelhouse Locally (`mykaggle uv make-wheel` / `make-wheel-strict`)
Because `torch`, `transformers`, `accelerate`, `peft`, and `datasets` are already pre-installed, **never download full transitive `torch` trees (~4 GB)**. Instead, build only missing wheels (including your repo `.` and local path dependencies) for Python 3.13 into `kaggle_wheelhouse`:

- `mykaggle uv make-wheel`: Automatically exports dependencies from `pyproject.toml` via `uv export` and filters out packages pre-installed on Kaggle by package name.
- `mykaggle uv make-wheel-strict`: Resolves dependencies using `uv pip compile` against exact `pkg==version` constraints from Kaggle (`kaggle_base_packages.txt`), keeping core GPU packages (`torch`, `triton`, `jax`, etc.) strictly pinned while automatically relaxing constraints for packages requiring newer/conflicting versions (e.g. newer `transformers` or `datasets`), and builds wheels for those conflicting packages and missing dependencies.

```bash
# Preview packages that would be built without downloading/building:
mykaggle uv make-wheel --dry-run [path]
mykaggle uv make-wheel-strict --dry-run [path]

# Build lightweight wheelhouse (defaults to ./kaggle_wheelhouse):
mykaggle uv make-wheel
mykaggle uv make-wheel-strict

# Specify a project path and/or custom output directory:
mykaggle uv make-wheel-strict /path/to/project -o dist_wheels
mykaggle uv make-wheel-strict dist_wheels

# Build all dependencies without filtering Kaggle base packages:
mykaggle uv make-wheel --full
mykaggle uv make-wheel-strict --full
```

### C. Upload / Version as a Kaggle Dataset (`kaggle` CLI)
```bash
# First time only (create dataset-metadata.json & dataset):
cat << 'EOF' > kaggle_wheelhouse/dataset-metadata.json
{
  "title": "My Training Wheelhouse",
  "id": "carlesoctav/my-wheelhouse",
  "licenses": [{"name": "CC0-1.0"}]
}
EOF
kaggle datasets create -p kaggle_wheelhouse --dir-mode skip

# Subsequent updates (push a new dataset version):
kaggle datasets version -p kaggle_wheelhouse -m "updated wheels" --dir-mode skip
```

### D. Dynamically Attach & Install with `uv`
```bash
# 1. Check /kaggle/input first (the slug may already have the dataset mounted):
mykaggle process run "ls -la /kaggle/input"

# 2. Attach (mounts at /kaggle/input/datasets/carlesoctav/my-wheelhouse and saves to kernel metadata):
mykaggle process attach carlesoctav/my-wheelhouse

# 3. Or refresh live when you push a new dataset version (e.g. version 3) without restarting the VM:
mykaggle process attach carlesoctav/my-wheelhouse -v 3

# (Note: you can also attach models, competitions, or other kernel outputs via --kernel / --competition)
mykaggle process attach google/gemma-4/Transformers/gemma-4-e4b/1
mykaggle process attach carlesoctav/other-kernel --kernel
mykaggle process attach gemma-4-developer-agent --competition

# 4. Install offline inside the VM using uv (and at the top of your kaggle/XX_*.py scripts):
mykaggle process run -n scratch "uv pip install --no-index --find-links=/kaggle/input/datasets/carlesoctav/my-wheelhouse my-pkg trl"
```

---

## 5. Experiment Tracking & Codebase Flow (`kaggle/`)

When working in a codebase, all Kaggle experiments and logs live in the `kaggle/` directory:

```text
my_project/
├── kaggle/
│   ├── logs.md                # Experiment changelog (version, script, config, hypothesis, metric)
│   ├── 01_baseline.py         # Self-contained Kaggle experiment script 1
│   ├── 01_baseline.log        # Captured log from interactive or batch run
│   └── 02_train_lr5.py        # Experiment script 2
├── src/                       # Local library code (packaged as wheel if needed)
└── pyproject.toml
```

### Rules for `kaggle/XX_<name>.py` Scripts
Every `kaggle/XX_<name>.py` script **must be self-contained and Kaggle-compatible**:
1. **Offline Wheel Bootstrap**: At the top of the script (before importing 3rd-party packages), check if required packages are installed; if missing and running offline, run `uv pip install --no-index --find-links=/kaggle/input/...` from the attached wheelhouse dataset.
2. **Input / Output Paths**: Read all datasets and models from `/kaggle/input/...` and write all checkpoints, artifacts, and logs to `/kaggle/working/...`.
3. **Multi-GPU Launching**: If training on multi-GPU (e.g., `4x NVIDIA L4`), launch or relaunch via `torchrun` or `accelerate` (e.g., `torchrun --nproc_per_node=4` or `accelerate launch --num_processes=4`).

### Standard Agent End-to-End Loop
1. **Start interactive VM**: `mykaggle session start <slug> -m l4`
2. **Check & attach datasets/wheels**: First inspect `/kaggle/input` (`mykaggle process run "ls -la /kaggle/input"`) as existing kernel slugs may already have the inputs mounted. Only run `mykaggle process attach <owner/wheelhouse>` or `mykaggle process attach <owner/dataset>` if an input is missing or needs to be refreshed to a newer version (`-v N`).
3. **Interactive debug loop**:
   - Write/edit `kaggle/01_xxx.py`.
   - Run interactively: `mykaggle process run -n scratch -f kaggle/01_xxx.py` (or detach with `-d` and monitor via `mykaggle process logs -n scratch -f`).
   - Save/tee logs to `kaggle/01_xxx.log` and fix any bugs immediately in the live session.
4. **Official Batch Commit**:
   - Once verified interactively, update `kaggle/logs.md`.
   - Trigger the batch commit via `mykaggle`:
     ```bash
     mykaggle process commit <slug> -f kaggle/01_xxx.py -m l4 -w -o ./output
     ```
   - Record the final status and metrics in `kaggle/logs.md`.
