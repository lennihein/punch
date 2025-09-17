{
  description = "Dev shell for ATOSS PDF parser";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.python312
            pkgs.python312Packages.pip
            pkgs.python312Packages.setuptools
            pkgs.python312Packages.wheel
            pkgs.python312Packages.venvShellHook
          ];

          venvDir = ".venv";

          postVenvCreation = ''
            pip install --upgrade pip
            if [ -f requirements.txt ]; then
              pip install -r requirements.txt
            fi
          '';

          postShellHook = ''
            source .venv/bin/activate
            echo "(nix) Activated venv: $VIRTUAL_ENV"
          '';
        };
      }
    );
}

