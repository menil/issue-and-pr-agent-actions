# nixpkgs is pinned so local development and CI use identical tool versions.
# With a floating channel, CI picked up a newer ruff than the one used locally
# and failed on rules the repo never opted into. Bump the revision and hash
# together, then run `just validate` before committing the new pin.
{ pkgs ? import
    (fetchTarball {
      url = "https://github.com/NixOS/nixpkgs/archive/d233902339c02a9c334e7e593de68855ad26c4cb.tar.gz";
      sha256 = "1485vqhb8cwym1m75v61i10j427vazszaklkwj2wmm80k8sijjyz";
    })
    { }
}:

# mypy and pytest must live in the same Python environment: mypy resolves
# `import pytest` in the test files from its own interpreter's site-packages,
# so installing them as separate top-level packages makes `mypy --strict`
# fail with import-not-found.
let
  pythonEnv = pkgs.python3.withPackages (ps: [
    ps.pytest
    ps.mypy
  ]);
in
pkgs.mkShell {
  buildInputs = [
    pkgs.just
    pkgs.ruff
    pythonEnv
  ];
}
