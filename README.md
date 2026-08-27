# NameRTS for SWE-bench

This repository extends
[NameRTS](https://github.com/ZJU-CTAG/NameRTS) with compatibility and runtime
support for applying regression test selection to the projects and Python
environments represented in
[SWE-bench](https://github.com/SWE-bench/SWE-bench).

NameRTS is a fine-grained regression test selection (RTS) technique for
Python. It models dependencies between Python code elements and names, then
selects tests reachable from the names affected by a patch. The core method is
described in:

> You Wang, Michael Pradel, and Zhongxin Liu. **Names Are All You Need:
> Effective and Safe Regression Test Selection for Python.** ISSTA 2026.
> [Paper](https://arxiv.org/abs/2605.25356)

For the original implementation, datasets, experimental results, and paper
replication workflow, use the
[upstream NameRTS repository](https://github.com/ZJU-CTAG/NameRTS).

## Why this specialized version exists

The standard SWE-bench harness normally runs only the tests associated with an
instance's reference pull request. A generated patch can therefore pass those
tests while introducing a regression elsewhere in the repository.

The companion SWE-bench RTS extension addresses this in two stages:

1. During one-time preparation, it runs the full test suite at the instance's
   base/setup parent while collecting NameRTS dynamic dependency information.
   It also runs the full suite once under the golden patch and records failures
   already present in the official environment.
2. During candidate evaluation, it reuses the prepared NameRTS cache, selects
   test files potentially affected by the candidate patch, and runs those tests
   inside a Docker image derived from the official SWE-bench image.

The harness then applies the following decision rule:

```text
new_regression_failures =
    candidate_selected_test_failures - golden_full_suite_failures

resolved =
    upstream_swebench_resolved
    and namerts_completed
    and (new_regression_failures is empty)
```

Artifact preparation, provenance validation, Docker image management, test
result parsing, and the final `resolved` decision belong to the SWE-bench
harness integration. This repository supplies the NameRTS analysis,
instrumentation, runtime capture support, and version-specific environments
used by that integration.

## Differences from upstream NameRTS

The name-based dependency propagation algorithm remains based on upstream
NameRTS. This fork adds compatibility and execution support needed for the much
broader range of projects and historical environments in SWE-bench:

- **Broader Python-version coverage.** The upstream replication setup targets
  Python 3.12 and 3.13. This fork provides analysis-environment specifications
  for Python 3.6 through 3.13 and includes source-level compatibility changes
  for older supported interpreters.
- **Matching analysis and test interpreters.** NameRTS analyzes Python
  bytecode, so the analysis environment must use the same Python minor version
  as the SWE-bench instance's `testbed`. Analysis dependencies remain isolated
  from the project's official test environment.
- **Framework-neutral dynamic capture.** Dependency capture can be installed
  through `sitecustomize` for a process that runs exactly one test file. It does
  not require injecting pytest hooks or modifying a target repository's
  `conftest.py`, which also enables projects with custom runners such as
  Django.
- **Initialization-time execution controls.** While collecting dynamic
  dependency information for the reusable NameRTS cache, test files can run in
  separate processes with bounded parallelism, captured output, and per-file
  timeouts. These controls are only needed for instrumented cache
  initialization. After NameRTS selects tests for a candidate patch, the
  SWE-bench harness executes the selected tests through the project's normal
  test path, without NameRTS instrumentation or per-file execution.
- **SWE-bench project compatibility.** The fork handles project-specific test
  targets, preserves the real checkout path used by official images, and
  includes robustness fixes for historical source trees—for example,
  semantically invalid relative imports stored as Pylint functional fixtures
  no longer abort analysis of the entire repository.
- **Reusable cache integration.** The analysis produces the cache members
  expected by the harness: `dependencies.json`, `py_checksums_cache.json`,
  `nbdp_cache.json`, and `critical_names.json`. The harness packages and binds
  these files to the instance, image, parent revision, Python version, and
  NameRTS source fingerprint.
- **SWE-bench validation utilities.** Additional scripts and reviewed data are
  included for validating NameRTS across representative SWE-bench projects and
  Python versions. They are development aids rather than the public harness
  entry point.

## Supported versions

The supported runtime range for the SWE-bench integration is Python 3.6
through 3.13, subject to a matching `environment_<version>.yml` file.

`environment_35.yml` and `environment_27.yml` document best-effort dependency
investigations for legacy SWE-bench instances. They are **not** supported
NameRTS runtimes: Python 3.5 still requires additional source and parser
backports, while Python 2.7 would require a substantial port of NameRTS and its
instrumentation stack. Such instances should use the ordinary SWE-bench
evaluation path without RTS.

## Repository layout

```text
src/namebdp.py              core name-based dependency propagation
src/parser.py               Python source analysis
src/bytecode.py             bytecode analysis helpers
src/import_collector.py     static import dependency collection
src/instrumentor/           lightweight dynamic instrumentor
src/runtime_bootstrap/      framework-neutral per-file dependency capture
environment_36.yml ...      version-specific NameRTS analysis environments
dataset/                    original NameRTS evaluation inputs
ground truth/               original and SWE-bench validation data
```

## Using this repository as a submodule

Add this repository at the path expected by the SWE-bench RTS integration, for
example:

```bash
git submodule add https://github.com/YOUR-ORGANIZATION/NameRTS-SWEB.git NameRTS
git submodule update --init --recursive
```

The harness builds a separately named `sweb.namerts.*` image derived from the
official instance image, copies this source tree into that image, creates the
matching NameRTS Conda environment, and installs only the lightweight
instrumentor in the official project `testbed`. Project tests are always run
inside that Docker environment.

Direct execution of the original paper experiments is still possible through
the inherited `src.evaluate` workflow, but the canonical instructions and
results for those experiments are maintained upstream. Users evaluating
SWE-bench patches should follow the documentation in the companion harness
repository.

## Attribution and license

This repository is derived from
[ZJU-CTAG/NameRTS](https://github.com/ZJU-CTAG/NameRTS). NameRTS is distributed
under the [Apache License 2.0](https://github.com/ZJU-CTAG/NameRTS/blob/main/LICENSE).
Please retain the upstream attribution when redistributing modified versions.
