{
  description = "pywebview via GTK/WebKitGTK in a uv shell (PyGObject from nixpkgs)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.uv
            pkgs.python312

            # Python GI bindings from nixpkgs (this provides `import gi`)
            pkgs.python312Packages.pygobject3

            # GI stack + GUI deps needed by pywebview GTK backend
            pkgs.gobject-introspection
            pkgs.glib
            pkgs.gtk3
            pkgs.webkitgtk_4_1  # or webkitgtk if your nixpkgs uses that attr

            # Helpful for native builds and GI discovery
            pkgs.pkg-config
          ];

          # Ensure GI can find typelibs and GSettings schemas at runtime
          # shellHook = ''
          #   export GI_TYPELIB_PATH="${pkgs.gobject-introspection}/lib/girepository-1.0:${pkgs.gtk3}/lib/girepository-1.0:${pkgs.webkitgtk_4_1}/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
          #   export XDG_DATA_DIRS="${pkgs.gsettings-desktop-schemas}/share/gsettings-schemas/${pkgs.gsettings-desktop-schemas.name}:${pkgs.gtk3}/share/gsettings-schemas/${pkgs.gtk3.name}:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
          # '';
        };
      });
}
