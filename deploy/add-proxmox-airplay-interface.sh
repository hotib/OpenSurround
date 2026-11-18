#!/bin/bash
set -euo pipefail
export LC_ALL=C

die() { printf 'add-proxmox-airplay-interface: %s\n' "$*" >&2; exit 1; }

if [[ ${1:-} == --help ]]; then
    cat <<'HELP'
Usage on the Proxmox node that owns the CT:
  add-proxmox-airplay-interface.sh CTID [netX]
Example:
  add-proxmox-airplay-interface.sh 123 net2
The default slot is net1. An existing matching ap2eth is reused in its
current slot. Set AIRPLAY_INTERFACE=ap2eth and the reserved same-LAN
IPv4 CIDR in the guest's /etc/opensurround/airplay-net.env.
HELP
    exit 0
fi

[[ $# -ge 1 && $# -le 2 && $1 =~ ^[1-9][0-9]{2,8}$ ]] ||
    die 'specify a CT ID (see --help)'
ctid=$1
slot=${2:-net1}
[[ $slot =~ ^net[1-9][0-9]?$ ]] || die 'specify a secondary slot such as net1'
node=$(hostname -s)
api=/nodes/$node/lxc/$ctid

# Read the digest with the same snapshot used to select the new NIC.
config=$(pvesh get "$api/config" --current 1 --output-format json)
pending=$(pvesh get "$api/pending" --output-format json)
perl -MJSON::PP -0777 -ne '
    for my $item (@{decode_json($_)}) {
        die "Resolve pending CT configuration changes before adding ap2eth.\n"
            if exists $item->{pending} || $item->{delete};
    }
' <<< "$pending"

plan=$(perl -MJSON::PP -0777 -e '
    sub nic { return { map { split /=/, $_, 2 } split /,/, $_[0] }; }

    my $slot = shift @ARGV;
    my $conf = decode_json(<STDIN>);
    my $base = nic($conf->{net0} // die "CT has no net0.\n");
    my $bridge = $base->{bridge} // die "net0 has no bridge.\n";
    my $tag = $base->{tag} // "";
    my $firewall = $base->{firewall} // 0;
    my @matches;
    for my $key (sort keys %$conf) {
        next unless $key =~ /^net[0-9]+$/;
        my $nic = nic($conf->{$key});
        next unless ($nic->{name} // "") eq "ap2eth";
        die "$key already uses ap2eth with incompatible settings.\n"
            unless $key ne "net0"
                && ($nic->{type} // "veth") eq "veth"
                && ($nic->{bridge} // "") eq $bridge
                && ($nic->{tag} // "") eq $tag
                && ($nic->{firewall} // 0) eq $firewall
                && ($nic->{ip} // "manual") eq "manual"
                && ($nic->{ip6} // "manual") eq "manual"
                && !grep { $nic->{$_} } qw(gw gw6 trunks link_down);
        push @matches, $key;
    }
    die "Multiple interfaces are named ap2eth.\n" if @matches > 1;
    if (@matches) {
        print "existing $matches[0]\n";
        exit;
    }
    die "$slot is occupied. Specify a free secondary slot.\n" if exists $conf->{$slot};
    my $digest = $conf->{digest} // "";
    die "Missing Proxmox configuration digest.\n" unless $digest =~ /^[0-9a-f]{40}$/;
    my $nic = "name=ap2eth,type=veth,bridge=$bridge,ip=manual,ip6=manual,firewall=$firewall";
    $nic .= ",tag=$tag" if length $tag;
    print "$digest $nic\n";
' "$slot" <<< "$config")

read -r digest nic <<< "$plan"
if [[ $digest == existing ]]; then
    printf 'ap2eth already configured in %s for CT %s\n' "$nic" "$ctid"
    exit 0
fi

pct set "$ctid" --digest "$digest" "--$slot" "$nic"
printf 'Configured ap2eth in %s for CT %s\n' "$slot" "$ctid"
