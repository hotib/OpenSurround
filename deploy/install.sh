#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    cat <<'HELP'
Usage: deploy/install.sh [options]
  --config FILE              Import a TOML baseline on first install
  --fifo FILE                Existing S32LE FIFO (default /tmp/surround.pcm)
  --channels 6|8             Input width (default 6, 8 requires an explicit profile)
  --web-host HOST            Listen address (default 0.0.0.0, for the trusted LAN)
  --web-port PORT            Web port (default 8080)
  --sendspin-state DIR       Import existing identity.key and pairings.json
  --airplay-address CIDR     Enable native PTP on a prepared sender-only LAN veth
  --airplay-interface NAME   Sender veth in the LXC (default ap2eth)
  --airplay-binary FILE      Use a trusted local sender binary on first install
  --airplay                  Enable AirPlay in non-PTP mode
  --no-airplay               Disable the optional AirPlay agent
  --no-start                 Install files without starting or enabling services
  --help                     Show this help
Omitted settings are retained on reinstall.
HELP
}

die() { printf '%s\n' "$*" >&2; exit 1; }

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
app=/opt/opensurround
etc=/etc/opensurround
state=/var/lib/opensurround
units=/etc/systemd/system
account=opensurround
router=opensurround.service
agent=opensurround-airplay.service
net=opensurround-airplay-net.service
dropin=$units/$agent.d/50-opensurround-ptp.conf

case ${1:-} in --help|-h) usage; exit 0 ;; esac
(( EUID == 0 )) || die 'Run as root inside the Debian LXC.'
exec 9>/run/lock/opensurround-install.lock
flock -n 9 || die 'Another OpenSurround installation is running.'

# Load only installer-owned, root-only settings. Values written below are
# restricted to characters that are literal in Bash and systemd EnvironmentFile.
[[ ! -f $etc/opensurround.env ]] || source "$etc/opensurround.env"
FIFO=${FIFO:-/tmp/surround.pcm}
CHANNELS=${CHANNELS:-6}
WEB_HOST=${WEB_HOST:-0.0.0.0}
WEB_PORT=${WEB_PORT:-8080}
AIRPLAY_MODE=${AIRPLAY_MODE:-ntp}
AIRPLAY_INTERFACE=ap2eth
AIRPLAY_ADDRESS=
[[ ! -f $etc/airplay-net.env ]] || source "$etc/airplay-net.env"

config= sendspin= binary= no_start=0 no_airplay=0 airplay=0 address_set=0 interface_set=0
while (($#)); do
    case $1 in
        --help|-h) usage; exit 0 ;;
        --no-start) no_start=1; shift; continue ;;
        --airplay) airplay=1; shift; continue ;;
        --no-airplay) no_airplay=1; shift; continue ;;
        --config|--fifo|--channels|--web-host|--web-port|--sendspin-state|--airplay-address|--airplay-interface|--airplay-binary)
            [[ ${2:-} && $2 != --* ]] || die "Missing value for $1" ;;
        *) die "Unknown option: $1" ;;
    esac
    case $1 in
        --config) config=$2 ;;
        --fifo) FIFO=$2 ;;
        --channels) CHANNELS=$2 ;;
        --web-host) WEB_HOST=$2 ;;
        --web-port) WEB_PORT=$2 ;;
        --sendspin-state) sendspin=$2 ;;
        --airplay-address) AIRPLAY_ADDRESS=$2; address_set=1 ;;
        --airplay-interface) AIRPLAY_INTERFACE=$2; interface_set=1 ;;
        --airplay-binary) binary=$2 ;;
    esac
    shift 2
done

[[ $FIFO =~ ^/[-a-zA-Z0-9_./+]+$ && -p $FIFO ]] || die 'Use an existing absolute FIFO path without spaces or shell metacharacters.'
[[ $CHANNELS == 6 || $CHANNELS == 8 ]] || die 'Use --channels 6 or 8.'
[[ $WEB_HOST =~ ^[-a-zA-Z0-9.:]+$ ]] || die 'Use a valid web listen address.'
[[ $WEB_PORT =~ ^[0-9]{1,5}$ ]] && ((10#$WEB_PORT >= 1 && 10#$WEB_PORT <= 65535)) || die 'Use a web port from 1 to 65535.'
if (( no_airplay )); then
    [[ $address_set == 0 && $interface_set == 0 && $airplay == 0 && ! $binary ]] || die 'AirPlay options conflict with --no-airplay.'
    AIRPLAY_MODE=off
elif (( address_set )); then
    (( ! airplay )) || die 'Use either --airplay or --airplay-address.'
    AIRPLAY_MODE=ptp
elif (( airplay )); then
    AIRPLAY_MODE=ntp
fi
[[ $interface_set == 0 || $AIRPLAY_MODE == ptp ]] || die '--airplay-interface requires native PTP.'
if [[ $AIRPLAY_MODE == ptp ]]; then
    [[ $AIRPLAY_INTERFACE =~ ^[a-zA-Z0-9][-a-zA-Z0-9_.]{0,14}$ ]] || die 'Invalid AirPlay interface name.'
    [[ $AIRPLAY_ADDRESS =~ ^[0-9.]+/[0-9]{1,2}$ ]] || die 'Use an IPv4 CIDR for --airplay-address.'
    python3 -c 'import ipaddress, sys; ipaddress.IPv4Interface(sys.argv[1])' "$AIRPLAY_ADDRESS" || die 'Invalid AirPlay IPv4 CIDR.'
    for file in airplay-net.sh "$net"; do
        [[ -f $repo/deploy/$file ]] || die "Native PTP requires deploy/$file."
    done
fi

profile=$state/profile/live.toml
if [[ $CHANNELS == 8 ]]; then
    profile=$state/profile/live-71.toml
fi
if [[ -e $profile && $config ]]; then
    printf 'Keeping the installed profile at %s.\n' "$profile"
    config=
fi
if [[ ! -e $profile && ! $config ]]; then
    [[ $CHANNELS == 6 ]] || die 'Supply an explicit 7.1 profile with --config.'
fi
[[ ! $config || -f $config ]] || die 'The baseline must be a regular file.'
[[ ! $binary || $AIRPLAY_MODE != off ]] || die 'Use --airplay to enable a custom sender.'
[[ ! $binary || -f $binary ]] || die 'The custom sender must be a regular file.'
if [[ $sendspin ]]; then
    [[ -f $sendspin/identity.key ]] || die 'The Sendspin state directory must contain identity.key.'
    if [[ -e $state/sendspin/identity.key ]]; then
        printf 'Keeping the installed Sendspin identity.\n'
        sendspin=
    fi
fi
if [[ $sendspin ]]; then
    if [[ -e $state/sendspin/pairings.json ]]; then
        cmp -s "$sendspin/pairings.json" "$state/sendspin/pairings.json" || die 'Existing Sendspin pairing store differs from the import.'
    fi
fi
[[ ! -e $app/venv || -L $app/venv ]] || die "Move the existing standalone $app/venv aside before installing."
for service in "$router" "$agent"; do
    fragment=$(systemctl show -p FragmentPath --value "$service" || true)
    if [[ $fragment && ! -f $etc/opensurround.env ]]; then
        die "Existing $service uses $fragment. Back up and remove its unit registration before installing."
    fi
done

packages=(python3-venv ca-certificates util-linux)
if [[ $AIRPLAY_MODE != off && ! -e $app/cliairplay && ! $binary ]]; then
    packages+=(curl)
fi
[[ $AIRPLAY_MODE != ptp ]] || packages+=(iproute2)
apt-get update
apt-get install -y --no-install-recommends "${packages[@]}"

if ! id "$account" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$state" --shell /usr/sbin/nologin "$account"
fi
runuser -u "$account" -- test -r "$FIFO" || die "Grant $account read access to $FIFO before installing."

install -d -m 0755 "$app" "$app/releases"
install -d -m 0750 -o root -g "$account" "$etc" "$state"
install -d -m 0700 -o "$account" -g "$account" "$state/profile" "$state/sendspin" "$state/airplay"

# Build beside the current venv. A failed build or checksum leaves it usable.
release=$(mktemp -d "$app/releases/release.XXXXXXXX")
trap 'if [[ -n $release ]]; then rm -rf -- "$release"; fi' EXIT
chmod 0755 "$release"
(
    umask 022
    python3 -m venv "$release"
    "$release/bin/python" -m pip install "$repo"
)
runuser -u "$account" -- "$release/bin/python" -m opensurround.cli --help >/dev/null
runuser -u "$account" -- "$release/bin/python" -m opensurround.airplay_agent --help >/dev/null

if [[ $AIRPLAY_MODE != off && ! -e $app/cliairplay ]]; then
    if [[ $binary ]]; then
        install -m 0755 "$binary" "$release/cliairplay"
    else
        case $(dpkg --print-architecture) in
            arm64) arch=aarch64; sha=ec1eba3f6aa26f435eb468d8fa4641b3bd84d12aa536839d95dcd8a3aeb6924e ;;
            amd64) arch=x86_64; sha=0b190b5422386d48f35529cd2e4bcd4851c700112dfa9b8cf542b1a13d47a7dc ;;
            *) die 'Unsupported architecture. Supply --airplay-binary or --no-airplay.' ;;
        esac
        curl --fail --silent --show-error --location --retry 2 \
            "https://github.com/music-assistant/airplay-cli/releases/download/v0.5.15/cliairplay-linux-$arch" \
            -o "$release/cliairplay"
        printf '%s  %s\n' "$sha" "$release/cliairplay" | sha256sum --check --status || die 'AirPlay sender checksum mismatch.'
        chmod 0755 "$release/cliairplay"
    fi
fi
if [[ $binary && -e $app/cliairplay ]]; then
    printf 'Keeping existing sender at %s/cliairplay.\n' "$app"
fi

if [[ ! -e $profile ]]; then
    if [[ $config ]]; then
        cp -- "$config" "$release/live.toml"
    else
        cat > "$release/live.toml" <<'TOML'
[server]
name = "OpenSurround Live"
host = "0.0.0.0"
port = 8928
auto_trust_unpaired = true
persist_pairings = false

[audio]
chunk_ms = 20
buffer_lead_ms = 300
TOML
    fi
    candidate=$release/live.toml
    validate_as=()
else
    candidate=$profile
    validate_as=(runuser -u "$account" --)
fi
"${validate_as[@]}" "$release/bin/python" - "$candidate" "$CHANNELS" <<'PY'
import sys
from opensurround.config import load_config

config = load_config(sys.argv[1], allow_empty=True)
for endpoint in config.endpoints:
    if any(channel >= int(sys.argv[2]) for channel in endpoint.source_channels):
        raise SystemExit(f"Route {endpoint.name} exceeds the selected input width.")
PY

if [[ -f $release/live.toml ]]; then
    install -m 0600 -o "$account" -g "$account" "$release/live.toml" "$profile"
    rm -- "$release/live.toml"
fi
if [[ $sendspin ]]; then
    for file in pairings.json identity.key; do
        [[ ! -f $sendspin/$file || -e $state/sendspin/$file ]] || install -m 0600 -o "$account" -g "$account" "$sendspin/$file" "$state/sendspin/$file"
    done
fi
if [[ ! -e $etc/web.token ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$release/web.token"
    install -m 0600 -o "$account" -g "$account" "$release/web.token" "$etc/web.token"
    rm -- "$release/web.token"
fi

if (( ! no_start )); then
    if systemctl is-active --quiet "$router"; then systemctl stop "$router"; fi
    if systemctl is-active --quiet "$agent"; then systemctl stop "$agent"; fi
    if [[ -f $dropin ]] && systemctl is-active --quiet "$net"; then systemctl stop "$net"; fi
fi

[[ ! -f $release/cliairplay ]] || mv -T -- "$release/cliairplay" "$app/cliairplay"
if [[ $AIRPLAY_MODE == off ]]; then
    airplay_args=
else
    airplay_args='"--airplay-socket /run/opensurround-airplay/agent.sock"'
fi
cat > "$etc/opensurround.env" <<ENV
FIFO=$FIFO
CHANNELS=$CHANNELS
PROFILE=$profile
WEB_HOST=$WEB_HOST
WEB_PORT=$WEB_PORT
AIRPLAY_MODE=$AIRPLAY_MODE
AIRPLAY_ARGS=$airplay_args
ENV
if [[ $AIRPLAY_MODE == ptp ]]; then
    install -m 0755 "$repo/deploy/airplay-net.sh" "$app/airplay-net.sh"
    install -m 0644 "$repo/deploy/$net" "$units/$net"
    cat > "$etc/airplay-net.env" <<ENV
AIRPLAY_INTERFACE=$AIRPLAY_INTERFACE
AIRPLAY_ADDRESS=$AIRPLAY_ADDRESS
ENV
    install -d -m 0755 "${dropin%/*}"
    cat > "$dropin" <<'UNIT'
[Unit]
Requires=opensurround-airplay-net.service
After=opensurround-airplay-net.service

[Service]
NetworkNamespacePath=/run/opensurround-airplay-net/netns
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=AIRPLAY_PTP=--ptp
UNIT
    chmod 0644 "$dropin"
else
    rm -f -- "$dropin"
fi

install -m 0644 "$repo/deploy/$router" "$repo/deploy/$agent" "$units/"

# Replace the symlink atomically, keeping the previous venv for rollback.
if [[ -L $app/venv ]]; then
    ln -s -- "$(readlink -f "$app/venv")" "$release/previous"
    mv -Tf -- "$release/previous" "$app/venv.previous"
fi
ln -s -- "$release" "$release/current"
mv -Tf -- "$release/current" "$app/venv"
release=
systemctl daemon-reload
if (( ! no_start )); then
    if [[ $AIRPLAY_MODE == off ]]; then
        systemctl disable "$agent"
    else
        systemctl enable --now "$agent"
    fi
    systemctl enable --now "$router"
fi
