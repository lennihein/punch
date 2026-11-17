{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      forEachSystem = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forEachSystem (pkgs: {
        default = pkgs.mkShellNoCC {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pdfminer-six ]))
            pkgs.typst
            pkgs.inter
          ];
          shellHook = ''
            export ATOSS_TYPST_FONT_PATH="${pkgs.inter}/share/fonts"
          '';
        };
      });
    };
}
