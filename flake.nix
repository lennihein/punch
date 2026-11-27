{
  description = "Punch! — Time-tracking webapp, ATOSS PDF parser/exporter, and Wear OS client";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forEachSystem = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      mkPythonEnv = pkgs: pkgs.python3.withPackages (ps: [
        ps.fastapi
        ps.uvicorn
        ps.jinja2
        ps.python-multipart
        ps.pdfminer-six
        ps.httpx
        ps.httpx2
        ps.pytest
      ]);

      mkPunchWeb = pkgs:
        let
          pythonEnv = mkPythonEnv pkgs;
          src = pkgs.lib.cleanSourceWith {
            src = self;
            filter = path: type:
              let
                base = baseNameOf (toString path);
              in
              !(pkgs.lib.hasSuffix ".pdf" base)
              && !(pkgs.lib.hasSuffix ".PDF" base)
              && !(pkgs.lib.hasSuffix ".typ" base)
              && base != "wear"
              && base != ".direnv"
              && base != ".git"
              && base != "__pycache__"
              && base != "atoss.db"
              && base != "atoss.db-wal"
              && base != "atoss.db-shm"
              && base != "punch.db"
              && base != "punch.db-wal"
              && base != "punch.db-shm"
              && !(pkgs.lib.hasSuffix "_26.txt" base);
          };
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "punch-web";
          version = "0.3.5";
          inherit src;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          dontBuild = true;

          installPhase = ''
            mkdir -p $out/lib/punch $out/bin
            cp -r app $out/lib/punch/
            cp cli.py parser_app.py parser_atoss.py table_utils.py validation.py $out/lib/punch/

            makeWrapper ${pythonEnv}/bin/uvicorn $out/bin/punch-web \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.typst ]} \
              --set PYTHONPATH "$out/lib/punch" \
              --set TYPST_FONT_PATH "${pkgs.inter}/share/fonts/truetype" \
              --add-flags "app.main:app --app-dir $out/lib/punch --workers 1"
          '';
        };
    in
    {
      packages = forEachSystem (pkgs: rec {
        punch-web = mkPunchWeb pkgs;
        default = punch-web;
      });

      devShells = forEachSystem (pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
          androidPkgs = import nixpkgs {
            inherit system;
            config = {
              allowUnfree = true;
              android_sdk.accept_license = true;
            };
          };
          androidComposition = androidPkgs.androidenv.composeAndroidPackages {
            platformVersions = [ "35" ];
            buildToolsVersions = [ "35.0.0" ];
          };
          androidSdkRoot = "${androidComposition.androidsdk}/libexec/android-sdk";
        in
        {
          default = pkgs.mkShellNoCC {
            packages = [
              (mkPythonEnv pkgs)
              pkgs.typst
              pkgs.inter
            ];
            TYPST_FONT_PATH = "${pkgs.inter}/share/fonts/truetype";
          };

          wear = pkgs.mkShell {
            packages = [
              androidPkgs.jdk17
              androidPkgs.gradle
              androidPkgs.android-tools
              androidComposition.androidsdk
            ];
            JAVA_HOME = "${androidPkgs.jdk17.home}";
            ANDROID_HOME = androidSdkRoot;
            ANDROID_SDK_ROOT = androidSdkRoot;
            GRADLE_OPTS = "-Dorg.gradle.project.android.aapt2FromMavenOverride=${androidSdkRoot}/build-tools/35.0.0/aapt2";
          };
        });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.punch;
          migrateScript = pkgs.writeShellScript "punch-migrate-db" ''
            if [ -e /var/lib/atoss/atoss.db ] && [ ! -e /var/lib/punch/punch.db ]; then
              mkdir -p /var/lib/punch/seed
              cp -a /var/lib/atoss/atoss.db /var/lib/punch/punch.db
              [ -e /var/lib/atoss/atoss.db-wal ] && cp -a /var/lib/atoss/atoss.db-wal /var/lib/punch/punch.db-wal
              [ -e /var/lib/atoss/atoss.db-shm ] && cp -a /var/lib/atoss/atoss.db-shm /var/lib/punch/punch.db-shm
              if [ -d /var/lib/atoss/seed ]; then
                cp -a /var/lib/atoss/seed/. /var/lib/punch/seed/
              fi
              chown -R punch:punch /var/lib/punch
              chmod 0750 /var/lib/punch
            fi
          '';
        in
        {
          options.services.punch = {
            enable = lib.mkEnableOption "Punch! time-tracking webapp service";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.punch-web;
              description = "The punch-web package to run.";
            };

            host = lib.mkOption {
              type = lib.types.str;
              default = "127.0.0.1";
              description = "Listen address for uvicorn.";
            };

            port = lib.mkOption {
              type = lib.types.port;
              default = 8000;
              description = "Listen port for uvicorn.";
            };

            databasePath = lib.mkOption {
              type = lib.types.str;
              default = "/var/lib/punch/punch.db";
              description = "Path to SQLite WAL database.";
            };

            timezone = lib.mkOption {
              type = lib.types.str;
              default = "Europe/Berlin";
              description = "Default timezone for users and server timestamps.";
            };

            trustProxyHeaders = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Trust Remote-User / Remote-Email headers from Authelia reverse proxy.";
            };

            seedReportPaths = lib.mkOption {
              type = lib.types.str;
              default = "";
              description = "Optional comma-separated paths to initial review report files to seed on first run.";
            };

            environmentFile = lib.mkOption {
              type = lib.types.nullOr lib.types.path;
              default = null;
              description = "Optional systemd EnvironmentFile (e.g. for LLM_API_KEY, LLM_PROVIDER, LLM_MODEL).";
            };
          };

          config = lib.mkIf cfg.enable {
            users.users.punch = {
              isSystemUser = true;
              group = "punch";
              home = "/var/lib/punch";
            };
            users.groups.punch = {};

            systemd.services.punch = {
              description = "Punch! FastAPI Service";
              after = [ "network.target" ];
              wantedBy = [ "multi-user.target" ];

              environment = {
                DATABASE_PATH = cfg.databasePath;
                TZ = cfg.timezone;
                TRUST_PROXY_HEADERS = if cfg.trustProxyHeaders then "true" else "false";
                SEED_REPORT_PATHS = cfg.seedReportPaths;
                PYTHONUNBUFFERED = "1";
              };

              serviceConfig = {
                User = "punch";
                Group = "punch";
                StateDirectory = "punch";
                StateDirectoryMode = "0750";
                WorkingDirectory = "/var/lib/punch";
                ExecStartPre = "+${migrateScript}";
                ExecStart = "${cfg.package}/bin/punch-web --host ${cfg.host} --port ${toString cfg.port} --proxy-headers";
                Restart = "on-failure";
                RestartSec = "3s";
                EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;

                ProtectSystem = "strict";
                ProtectHome = true;
                PrivateTmp = true;
                PrivateDevices = true;
                NoNewPrivileges = true;
                CapabilityBoundingSet = "";
                RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
                ReadWritePaths = [ "/var/lib/punch" ];
              };
            };
          };
        };
    };
}
