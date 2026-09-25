# Atlas native-backtest isolation

Atlas preserves upstream `BacktestTool -> Runner -> backtest/runner.py` and the
native loaders, strategy validation, engines, and result files. The application
server runs as `vibe`; a small local broker executes only that fixed operation
as `vibe-sandbox`. Neither HTTP requests nor the model can supply an interpreter,
entry script, shell command, or privilege target to the broker.

The broker validates run paths under the persistent run roots, rejects symlinks
and hard links inside runs, authenticates the caller by Unix peer UID, and allows
only one generated-code job at a time. The child has no supplementary groups,
no provider/API/broker credentials in its environment, a temporary HOME/cache,
resource limits, and Linux Landlock ABI 3 or later. App source and dependencies
are read-only; private settings, sessions, other runs, and process environments
are outside the filesystem allowlist. Only the current run and temporary scratch
are writable. Uploaded/imported datasets and data-bridge configuration are
explicit read-only inputs. Upstream market-data credentials remain available to
their loaders. Local data paths outside approved import/run roots are refused;
move those datasets into the app's persistent uploads/imports first.

The trusted broker/supervisor retains root only to start the restricted child,
prepare those exact run permissions, and kill/reap its process tree. The HTTP
server and generated code never run as root. Source and the interpreter are
root-owned. Shell/background agent tools remain disabled in Atlas.

## Verification

`python -m unittest discover -s agent/tests -p test_atlas_sandbox.py -v` covers
malicious paths, request fields, environment filtering, absent-broker refusal,
native Runner routing, output collection, and timeout propagation.

Each container start additionally checks actual sandbox UID and filesystem
denials using synthetic credential/sibling files, allowed output/cache writes,
and source read-only behavior. It executes a real upstream native backtest over
a small synthetic local OHLCV dataset and requires its equity, metrics, and trade
artifacts. It then tests termination of a timed-out process with a detached
child. Only Boolean results are logged in `ATLAS_ISOLATION`; no secret values
are printed. Failure or an unsupported kernel prevents the web server from
starting. Passing Windows unit tests is not proof of the Linux boundary; inspect
the deployed startup result and perform a normal native research backtest.

## Boundary and limitations

This is filesystem/process isolation, not network isolation. Native data loaders
retain network access, including technical reachability of the Railway private
network. The upstream AST guard remains in place. Private service authentication
must not rely solely on the shared network being private. No live trading is
enabled by this change. The shared sandbox UID is reserved for the broker and
jobs are serialized so cleanup can remove even detached descendants safely.

Temporary sandbox homes and caches are removed after each run. Native final
artifacts stay in the application's existing persistent run directory. No new
service, public link, provider configuration, or repository is required.
