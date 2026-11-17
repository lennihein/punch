# NixOS module for the atoss webapp (webapp.py).
#
# Usage in configuration.nix:
#   imports = [ (import /path/to/atoss/module.nix { src = /path/to/atoss; }) ];
#   services.atoss-web.enable = true;
{ src }:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.atoss-web;
  pythonEnv = pkgs.python3.withPackages (ps: [ ps.pdfminer-six ]);
in
{
  options.services.atoss-web = {
    enable = lib.mkEnableOption "atoss PDF merge webapp";

    port = lib.mkOption {
      type = lib.types.port;
      default = 8088;
      description = "Loopback port the app listens on; Caddy proxies to it.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/atoss-web";
      description = "Persistent storage for uploads and reports (per user).";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "atoss-web";
      description = "System user the service runs as.";
    };

    caddy = {
      enable = lib.mkEnableOption "manage a Caddy virtualHost with Authelia forward auth" // {
        default = false;
      };

      domain = lib.mkOption {
        type = lib.types.str;
        example = "times.example.com";
        description = "Public hostname for the app.";
      };

      autheliaAddress = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1:9091";
        description = "Address of the Authelia verify endpoint.";
      };

      autheliaLoginDomain = lib.mkOption {
        type = lib.types.str;
        example = "auth.example.com";
        description = "Domain of the Authelia login portal, used as the rd parameter.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    users.groups.${cfg.user} = { };
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.user;
    };

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.user} -"
    ];

    systemd.services.atoss-web = {
      description = "atoss PDF merge webapp";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];
      environment = {
        ATOSS_DATA_DIR = cfg.dataDir;
        ATOSS_HOST = "127.0.0.1";
        ATOSS_PORT = toString cfg.port;
        ATOSS_TYPST_BIN = "${pkgs.typst}/bin/typst";
        ATOSS_TYPST_FONT_PATH = "${pkgs.inter}/share/fonts";
        PATH = lib.makeBinPath [ pythonEnv pkgs.typst pkgs.coreutils ];
      };
      serviceConfig = {
        User = cfg.user;
        Group = cfg.user;
        ExecStart = "${pythonEnv}/bin/python ${src}/webapp.py";
        Restart = "on-failure";
        RestartSec = 2;
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.dataDir ];
      };
    };

    services.caddy.virtualHosts = lib.mkIf (cfg.caddy.enable && (config.services.caddy.enable or false)) {
      ${cfg.caddy.domain} = {
        extraConfig = ''
          forward_auth ${cfg.caddy.autheliaAddress} {
            uri /api/verify?rd=https://${cfg.caddy.autheliaLoginDomain}/
            copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
          }
          reverse_proxy 127.0.0.1:${toString cfg.port}
        '';
      };
    };
  };
}
