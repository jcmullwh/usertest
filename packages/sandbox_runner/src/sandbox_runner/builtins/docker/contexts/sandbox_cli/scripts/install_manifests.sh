#!/bin/sh
set -eu

manifests_dir="${1:-/manifests}"

apt_manifest="$manifests_dir/apt.txt"
pip_manifest="$manifests_dir/pip.txt"
npm_manifest="$manifests_dir/npm-global.txt"

if [ -f "$apt_manifest" ]; then
    apt_packages="$(grep -Ev '^[[:space:]]*(#|$)' "$apt_manifest" | tr '\n' ' ' | tr -s ' ')"
    if [ -n "${apt_packages:-}" ]; then
        apt-get update
        if echo " $apt_packages " | grep -Eq '(^|[[:space:]])nodejs([[:space:]]|$)'; then
            # Debian repos typically ship an older Node.js. Some agent CLIs (e.g., Gemini CLI)
            # require a newer Node runtime, so prefer the NodeSource LTS repo when Node is needed.
            #
            # Note: this is executed during Docker image build; it does not run at usertest runtime.
            apt-get install -y --no-install-recommends bash ca-certificates curl gnupg
            # Avoid `curl | bash` so we can detect download failures and keep logs intelligible.
            tmp="/tmp/nodesource_setup.sh"
            rm -f "$tmp"
            if curl -fsSL https://deb.nodesource.com/setup_20.x -o "$tmp"; then
                if ! bash "$tmp"; then
                    echo "Warning: NodeSource setup script failed; falling back to Debian nodejs." >&2
                fi
            else
                echo "Warning: Failed to download NodeSource setup script; falling back to Debian nodejs." >&2
            fi
            rm -f "$tmp"
        fi
        apt-get install -y --no-install-recommends $apt_packages
    fi
fi

if command -v python >/dev/null 2>&1 && [ -f "$pip_manifest" ]; then
    # pip supports comments/blank lines in -r files; install only if the file has content.
    if grep -Eqv '^[[:space:]]*(#|$)' "$pip_manifest"; then
        python -m pip install --no-cache-dir -r "$pip_manifest"
    fi
fi

if [ -f "$npm_manifest" ]; then
    npm_packages="$(grep -Ev '^[[:space:]]*(#|$)' "$npm_manifest" | tr '\n' ' ' | tr -s ' ')"
    if [ -n "${npm_packages:-}" ]; then
        if ! command -v npm >/dev/null 2>&1; then
            echo "npm is required to install npm-global packages but was not found; attempting to install it via apt." >&2
            apt-get update
            apt-get install -y --no-install-recommends npm
        fi
        if ! command -v npm >/dev/null 2>&1; then
            echo "Error: npm is still unavailable; cannot install: $npm_packages" >&2
            exit 1
        fi
        npm install -g $npm_packages
    fi
fi
