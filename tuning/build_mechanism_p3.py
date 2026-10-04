#!/usr/bin/env python3
"""Build the isolated P3 engine using three exact, nonpersistent Go overlays.

Frozen source bytes and the default build remain unchanged. Only newly named
artifacts under build/mechanism-p3 and bin/lorentz-mechanism-p3 are written.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OVERLAY_SOURCES = ["strategy/run.go", "cmd/lorentz/robust_eval.go", "cmd/lorentz/main.go"]


def replace_once(text, before, after, path):
    if text.count(before) != 1:
        raise RuntimeError(f"overlay anchor mismatch: {path}")
    return text.replace(before, after, 1)


def prepare_overlay():
    out = ROOT / "build/mechanism-p3"
    out.mkdir(parents=True, exist_ok=True)
    replacements = {}
    for name in OVERLAY_SOURCES:
        src = ROOT / name
        text = src.read_text()
        if name == "strategy/run.go":
            text = replace_once(text, "func run(rows, daily []market.Candle, c Config, initialization bool) (Result, error) {", "func run(rows, daily []market.Candle, c Config, initialization bool) (Result, error) {\n return runWithMechanism(rows,daily,c,initialization,nil)\n}\nfunc runWithMechanism(rows, daily []market.Candle, c Config, initialization bool, mechanismRunner mechanismInputRunner) (Result, error) {", name)
            text = replace_once(text, "p, e = indicator.RunExtendedInputs(x, c.Classic, vectors)", "if mechanismRunner == nil {\n p, e = indicator.RunExtendedInputs(x, c.Classic, vectors)\n } else {\n p, e = mechanismRunner(x,c.Classic,vectors)\n }", name)
        elif name == "cmd/lorentz/robust_eval.go":
            text = replace_once(text, "aggregation market.DailyAggregation\n}", "aggregation market.DailyAggregation\n runner func([]market.Candle, []market.Candle, strategy.Config) (strategy.Result,error)\n}", name)
            text = replace_once(text, "r, e := strategy.RunRobust(ds.rows, ds.daily[begin:stop], c)", "runner := ds.runner\n if runner == nil { runner = strategy.RunRobust }\n r, e := runner(ds.rows, ds.daily[begin:stop], c)", name)
        else:
            text = replace_once(text, 'case "robust-eval":', 'case "mechanism-eval":\n err = mechanismEval(os.Args[2:])\n case "mechanism-config-validate":\n err = mechanismConfigValidate(os.Args[2:])\n case "robust-eval":', name)
        dst = out / name.replace("/", "_")
        dst.write_text(text)
        replacements[str(src)] = str(dst)
    overlay = out / "overlay.json"
    overlay.write_text(json.dumps({"Replace": replacements}, indent=2) + "\n")
    return overlay


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependency_inputs(overlay, env):
    """Collect the repository sources selected by the actual tagged Go build."""
    raw = subprocess.check_output(
        ["go", "list", "-deps", "-json", "-buildvcs=false",
         "-tags=mechanism_p3", f"-overlay={overlay}", "./cmd/lorentz"],
        cwd=ROOT, env=env, text=True,
    )
    decoder = json.JSONDecoder()
    files = set()
    while raw.strip():
        package, end = decoder.raw_decode(raw.lstrip())
        raw = raw.lstrip()[end:]
        directory = Path(package["Dir"])
        if not directory.is_relative_to(ROOT):
            continue  # The toolchain is bound separately by go_version.
        for kind in ("GoFiles", "CgoFiles", "CFiles", "CXXFiles", "MFiles",
                     "HFiles", "FFiles", "SFiles", "SysoFiles", "EmbedFiles"):
            files.update(directory / name for name in package.get(kind, []))
    files.add(Path(__file__).resolve())
    for name in ("go.mod", "go.sum"):
        if (ROOT / name).is_file():
            files.add(ROOT / name)
    files.add(overlay)
    mapping = json.loads(overlay.read_text())["Replace"]
    files.update(Path(name) for name in mapping)
    files.update(Path(name) for name in mapping.values())
    return sorted(files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    original_bytes = {ROOT / name: (ROOT / name).read_bytes()
                      for name in OVERLAY_SOURCES}
    overlay = prepare_overlay()
    env = dict(os.environ)
    env.setdefault("GOCACHE", "/tmp/lorentz-mechanism-p3-gocache")
    bound_before = {str(path): sha256(path)
                    for path in dependency_inputs(overlay, env)}
    (ROOT / "bin").mkdir(exist_ok=True)
    binary = ROOT / "bin/lorentz-mechanism-p3"
    command = ["go", "build", "-buildvcs=false", "-tags=mechanism_p3",
               f"-overlay={overlay}", "-o", str(binary), "./cmd/lorentz"]
    try:
        subprocess.run(command, cwd=ROOT, env=env, check=True)
    finally:
        if any(path.read_bytes() != before
               for path, before in original_bytes.items()):
            raise RuntimeError("frozen overlay input changed during build")
    if any(sha256(Path(path)) != before
           for path, before in bound_before.items()):
        raise RuntimeError("bound build input changed during build")
    manifest = {
        "version": "mechanism-p3-build-v1",
        "binary": {"path": str(binary), "sha256": sha256(binary)},
        "bound_files": bound_before,
        "go_version": subprocess.check_output(
            ["go", "version"], cwd=ROOT, env=env, text=True).strip(),
        "command": command,
        "overlay_original_sources_unchanged": True,
    }
    manifest_path = overlay.parent / "build-manifest.json"
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary.replace(manifest_path)
    if args.test:
        subprocess.run(["go", "test", "-tags=mechanism_p3", f"-overlay={overlay}",
                        "./indicator", "./strategy", "./cmd/lorentz"],
                       cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    main()
