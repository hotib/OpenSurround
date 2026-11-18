#!/bin/bash
set -euo pipefail
export LC_ALL=C

die() { printf 'opensurround-airplay-net: %s\n' "$*" >&2; exit 1; }

if [[ ${1:-} == --help ]]; then
    cat <<'HELP'
Run through opensurround-airplay-net.service. Set these literal values in
/etc/opensurround/airplay-net.env (use your own reserved LAN address):
  AIRPLAY_INTERFACE=ap2eth
  AIRPLAY_ADDRESS=192.0.2.42/24
Use a dedicated veth with no configured IP or routes.
HELP
    exit 0
fi
[[ $# == 0 ]] || die 'expected no arguments (see --help)'

iface=${AIRPLAY_INTERFACE:-}
address=${AIRPLAY_ADDRESS:-}
[[ $iface =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}$ ]] ||
    die 'AIRPLAY_INTERFACE must be a local interface name of up to 15 characters'
[[ $address =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/(0|[1-9][0-9]?)$ ]] ||
    die 'AIRPLAY_ADDRESS must be an IPv4 CIDR'
(( ${address#*/} <= 32 )) || die 'IPv4 prefix must be between 0 and 32'
IFS=. read -r -a octets <<< "${address%/*}"
for octet in "${octets[@]}"; do
    [[ $octet == 0 || $octet != 0* ]] && (( octet <= 255 )) ||
        die 'IPv4 octets must be canonical decimal values between 0 and 255'
done

link=$(ip -o link show dev "$iface" type veth)
[[ -n $link && $link != *' master '* ]] || die "$iface must be a dedicated veth"
ipv4=$(ip -o -4 addr show dev "$iface")
ipv6=$(ip -o -6 addr show dev "$iface" scope global)
routes4=$(ip -4 route show table all dev "$iface")
# The kernel can create IPv6 link-local and multicast routes on an unused veth.
routes6=$(ip -6 route show table all dev "$iface" | awk '$0 !~ / proto kernel( |$)/')
[[ -z $ipv4 && -z $ipv6 && -z $routes4 && -z $routes6 ]] ||
    die "$iface already has an address or configured route"

parent_ns=$(readlink "/proc/$$/ns/net")
path=/run/opensurround-airplay-net/netns
keeper=
move_attempted=0
published=0
in_namespace() { nsenter --target "$keeper" --net -- "$@"; }

cleanup() {
    local status=$?
    trap '' TERM INT
    if (( published )); then
        rm -f -- "$path" || status=1
    fi
    if (( move_attempted )) && in_namespace ip link show dev "$iface" >/dev/null 2>&1; then
        if in_namespace ip link set dev "$iface" netns "$$"; then
            if [[ $link =~ [\<,]UP[,\>] ]]; then
                ip link set dev "$iface" up || status=1
            fi
        else
            printf 'opensurround-airplay-net: could not return %s to the original namespace\n' "$iface" >&2
            status=1
        fi
    fi
    if [[ -n $keeper ]]; then
        kill "$keeper" 2>/dev/null || true
        wait "$keeper" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 0' TERM INT

unshare --net -- sleep infinity &
keeper=$!
keeper_path=/proc/$keeper/ns/net
for ((attempt=0; attempt<50; attempt++)); do
    kill -0 "$keeper" 2>/dev/null || die 'could not start the namespace keeper'
    keeper_ns=$(readlink "$keeper_path" 2>/dev/null || true)
    [[ -n $keeper_ns && $keeper_ns != "$parent_ns" ]] && break
    sleep 0.1
done
[[ -n $keeper_ns && $keeper_ns != "$parent_ns" ]] || die 'timed out waiting for the new network namespace'

move_attempted=1
ip link set dev "$iface" netns "$keeper"
in_namespace ip link set dev lo up
in_namespace ip -4 address add "$address" dev "$iface"
in_namespace ip link set dev "$iface" up
# PID 1 joins via this path; no namespace bind mount is needed.
ln -sT -- "$keeper_path" "$path"
published=1
systemd-notify --ready

wait "$keeper" || true
keeper=
die 'namespace keeper exited unexpectedly'
