# Mirror Operator Demo

Ansible automation for a KVM hypervisor lab on AWS bare metal. The top-level playbook provisions an EC2 host, configures KVM/GNOME/VNC, creates an isolated libvirt network, and deploys a RHEL 9 mirror workstation plus demo VMs with iDRAC-like BMCs (per-VM IP: Redfish + IPMI/SOL).

After the hypervisor lab, root [`site.yml`](site.yml) imports [`playbooks/openshift/site.yml`](playbooks/openshift/site.yml) as a single sub-playbook (same file you can run alone for cluster-only work).

## Quick start

```bash
./setup.sh
source .venv/bin/activate
aws configure   # if needed

ansible-playbook site.yml \
  -e vnc_password="..." \
  -e rhel_vm_password="..." \
  -e rhel_vm_root_password="..." \
  -e rhel_iso_url="https://access.cdn.redhat.com/..."
```

Ansible reads [`ansible.cfg`](ansible.cfg): inventory [`inventory/hosts.yml`](inventory/hosts.yml), remote user `ec2-user`, host key checking disabled.

## Playbook flow

[`site.yml`](site.yml) runs the hypervisor lab, then the OpenShift stack:

**Hypervisor lab**

1. [`playbooks/provision-ec2.yml`](playbooks/provision-ec2.yml) — launch bare-metal EC2 instance
2. [`playbooks/configure-host.yml`](playbooks/configure-host.yml) — KVM, GNOME, VNC, Cockpit, NTP, DNS
3. [`playbooks/setup-proxy.yml`](playbooks/setup-proxy.yml) — corporate MITM proxy (forward mode first) and PKI
4. [`playbooks/create-networks.yml`](playbooks/create-networks.yml) — default NAT + isolated libvirt network
5. [`playbooks/create-storage.yml`](playbooks/create-storage.yml) — libvirt storage pool and ISO directory
6. [`playbooks/create-fedora-vm.yml`](playbooks/create-fedora-vm.yml) — RHEL 9 mirror workstation (FIPS + DISA STIG)
7. [`playbooks/create-bare-vm.yml`](playbooks/create-bare-vm.yml) — bare VM with extra disk
8. [`playbooks/create-sno-vm.yml`](playbooks/create-sno-vm.yml) — SNO demo VM
9. [`playbooks/setup-bmc.yml`](playbooks/setup-bmc.yml) — per-VM BMC (Redfish + IPMI/SOL on one IP, iDRAC-like)

**OpenShift** (via [`playbooks/openshift/site.yml`](playbooks/openshift/site.yml))

10. [`connect-cluster.yml`](playbooks/openshift/connect-cluster.yml) — kubeconfig, version/channel facts
11. [`configure-cluster-proxy.yml`](playbooks/openshift/configure-cluster-proxy.yml) — trust bundle + proxy URLs, then [`enable-proxy-mitm.yml`](playbooks/enable-proxy-mitm.yml)
12. [`configure-cert-manager-issuer.yml`](playbooks/openshift/configure-cert-manager-issuer.yml) — ClusterIssuer
13. [`scale-worker-machineset.yml`](playbooks/openshift/scale-worker-machineset.yml) — worker MachineSet replicas
14. [`create-gp3-high-perf-sc.yml`](playbooks/openshift/create-gp3-high-perf-sc.yml) — high-performance `StorageClass`
15. [`install-odf-noobaa.yml`](playbooks/openshift/install-odf-noobaa.yml) — ODF / NooBaa operator install
16. [`configure-noobaa-backing-store.yml`](playbooks/openshift/configure-noobaa-backing-store.yml) — NooBaa backing store
17. [`validate-odf-noobaa.yml`](playbooks/openshift/validate-odf-noobaa.yml) — health checks

All configuration flows from [`group_vars/all.yml`](group_vars/all.yml), inventory, CLI extra-vars (`-e`), and external prerequisites. There is no playbook-level `vars:` block in `site.yml`.

## Required external inputs

These are not set in `group_vars/all.yml` and must be provided at runtime:

| Input | Source | Used by |
|-------|--------|---------|
| **AWS credentials** | `aws configure` or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION` | `provision-ec2.yml` |
| **`rhel_iso_url`** | Pass via `-e` (default is empty) | `create-fedora-vm.yml` — downloads RHEL 9 DVD ISO |
| **Python venv + collections** | `./setup.sh` installs ansible, boto3, and collections from [`requirements.yml`](requirements.yml) | All playbooks |

**Strongly recommended overrides** (defaults are `"changeme"`):

- `vnc_password` — VNC password and `ec2-user` Cockpit login
- `rhel_vm_password` / `rhel_vm_root_password` — RHEL workstation kickstart credentials

## Inventory

[`inventory/hosts.yml`](inventory/hosts.yml) defines an empty `hypervisors` group. **Do not commit static EC2 public IPs** — they go stale when instances are terminated.

- **Full `site.yml` run:** `provision-ec2.yml` dynamically adds the live EC2 public IP to `hypervisors` via `add_host`.
- **Re-run child playbooks only** (skip `provision-ec2.yml`): [`playbooks/discover-hypervisors.yml`](playbooks/discover-hypervisors.yml) looks up running instances tagged `Name={{ ec2_instance_name }}` and `add_host`s them (needs AWS credentials + `keys/{{ ec2_key_name }}.pem`). `setup-bmc.yml` imports this automatically. For other playbooks:

```bash
ansible-playbook playbooks/discover-hypervisors.yml playbooks/create-bare-vm.yml
```

Or temporarily add the current public IP to `inventory/hosts.yml`.

If you see `UNREACHABLE` on an old IP like `18.218.36.100`, that host was left in inventory from a previous session. Clear `inventory/hosts.yml` and re-run, or limit to the current host:

```bash
ansible-playbook site.yml --limit <current-ec2-ip> ...
```

No inventory `group_vars` files exist — all variables come from `group_vars/all.yml`.

## Variables reference

All child playbooks load [`group_vars/all.yml`](group_vars/all.yml) via `vars_files`. Override any variable with `-e key=value` or ansible-vault.

### EC2 provisioning (`provision-ec2.yml`)

| Variable | Default | Notes |
|----------|---------|-------|
| `ec2_instance_type` | `m5.metal` | Bare-metal instance |
| `ec2_root_volume_size` | `1500` | GB |
| `ec2_root_volume_type` | `gp3` | |
| `ec2_root_volume_iops` | `6000` | |
| `ec2_root_volume_throughput` | `400` | MB/s |
| `ec2_instance_name` | `mirror-operator-hypervisor` | |
| `ec2_instance_count` | `1` | Values >1 append `-1`, `-2`, etc. |
| `ec2_key_name` | `mirror-operator-key` | EC2 key pair name |
| `ec2_key_local_path` | `{{ playbook_dir }}/../keys` | Private key saved here |
| `ec2_security_group_name` | `mirror-operator-sg` | Opens SSH 22, VNC 5901, Cockpit 9090, proxy 3128 |
| `ec2_vpc_domain_pattern` | `*.opentlc.com` | VPC discovery tag filter |

Launch tries **one public subnet per availability zone** in sorted AZ order. If AWS returns `InsufficientInstanceCapacity`, the playbook automatically retries the next AZ with a public subnet in the same VPC.

**Runtime-derived (not user inputs):** `ec2_ami`, `ec2_ami_name` (latest RHEL 9 Hourly GP3 AMI), `ec2_vpc_id`, `ec2_subnet_id`, `ec2_launch_availability_zone`, `ec2_instance_names`.

### Host configuration (`configure-host.yml`)

| Variable | Default | Notes |
|----------|---------|-------|
| `vnc_password` | `"changeme"` | VNC + ec2-user password |
| `vnc_display` | `1` | VNC on port 5901 |
| `vnc_geometry` | `"1920x1080"` | |
| `ntp_allowed_network` | `192.168.100.0/24` | chrony allow ([`templates/chrony.conf.j2`](templates/chrony.conf.j2)) |
| `isolated_network_dns_records` | mirror/api/apps hostnames | Host dnsmasq ([`templates/host-dnsmasq.conf.j2`](templates/host-dnsmasq.conf.j2)) |

Default DNS records in `group_vars/all.yml`:

| Hostname | IP |
|----------|-----|
| `proxy.example.com` | `192.168.100.1` |
| `mirror.example.com` | `192.168.100.10` |
| `api.c0.example.com` | `192.168.100.11` |
| `apps.c0.example.com` | `192.168.100.11` (wildcard) |
| `api.sno.example.com` | `192.168.100.12` |
| `apps.sno.example.com` | `192.168.100.12` (wildcard) |

### Isolated libvirt network (`create-networks.yml`)

| Variable | Default |
|----------|---------|
| `isolated_network_name` | `isolated` |
| `isolated_network_bridge` | `virbr-isolated` |
| `isolated_network_subnet` | `192.168.100.0` |
| `isolated_network_prefix` | `24` |
| `isolated_network_gateway` | `192.168.100.1` |
| `isolated_network_dhcp_start` | `192.168.100.100` |
| `isolated_network_dhcp_end` | `192.168.100.200` |
| `isolated_network_dns_records` | See table above |

Rendered by [`templates/isolated-network.xml.j2`](templates/isolated-network.xml.j2).

### Storage pool (`create-storage.yml`)

| Variable | Default |
|----------|---------|
| `storage_pool_name` | `default` |
| `storage_pool_path` | `/var/lib/libvirt/images` |
| `iso_storage_path` | `/var/lib/libvirt/images/iso` |

### RHEL 9 mirror workstation (`create-fedora-vm.yml`)

| Variable | Default | Required? |
|----------|---------|-----------|
| `rhel_vm_name` | `rhel-workstation` | |
| `rhel_vm_vcpus` | `8` | |
| `rhel_vm_memory_mb` | `32768` | |
| `rhel_vm_disk_size_gb` | `750` | |
| `rhel_iso_url` | `""` | **Yes — must override** |
| `rhel_iso_filename` | `rhel-9-dvd.iso` | |
| `rhel_vm_ip` | `192.168.100.10` | Static IP in kickstart |
| `rhel_vm_user` | `admin` | |
| `rhel_vm_password` | `"changeme"` | Override recommended |
| `rhel_vm_root_password` | `"changeme"` | Override recommended |

Kickstart is rendered from [`templates/rhel-kickstart.cfg.j2`](templates/rhel-kickstart.cfg.j2). The VM installs on the `default` NAT network, then switches to `isolated_network_name`.

### Bare VM (`create-bare-vm.yml`)

| Variable | Default |
|----------|---------|
| `bare_vm_name` | `bare-vm` |
| `bare_vm_vcpus` | `18` |
| `bare_vm_memory_mb` | `51200` |
| `bare_vm_disk_size_gb` | `120` |
| `bare_vm_extra_disk_size_gb` | `300` |

### SNO VM (`create-sno-vm.yml`)

| Variable | Default |
|----------|---------|
| `sno_vm_name` | `sno-vm` |
| `sno_vm_vcpus` | `10` |
| `sno_vm_memory_mb` | `24576` |
| `sno_vm_disk_size_gb` | `120` |

### Per-VM BMC / iDRAC-like (`setup-bmc.yml`)

Each managed libvirt VM gets its own BMC IP on the isolated bridge. **Redfish and IPMI/SOL share that IP** (like iDRAC): HTTPS `:443` and IPMI UDP `:623`. One `sushy-emulator@<vm>` process per node (`SUSHY_EMULATOR_ALLOWED_INSTANCES` = that VM only) plus VirtualBMC bound to the same address.

VirtualBMC is installed from **GitHub `openstack/virtualbmc` master** (`vbmc_pip_package`), not PyPI 3.3.0, because only master ships Serial-over-LAN (`sol.py` + `activate_payload`). `setup-bmc.yml` recreates `/opt/virtualbmc` with Python ≥3.11 when the venv is missing, below 3.11, or lacks `vbmc`/`vbmcd`. Disconnect `virsh console` before `sol activate`. `sol payload status/enable` and `sol info`/`sol set` are still unimplemented upstream.

| Variable | Default |
|----------|---------|
| `bmc_nodes` | `bare-vm` → `192.168.100.50`, `sno-vm` → `192.168.100.51` |
| `bmc_redfish_port` | `443` |
| `bmc_ipmi_port` | `623` |
| `bmc_username` / `bmc_password` | `admin` / `"changeme"` |
| `vbmc_install_dir` | `/opt/virtualbmc` |
| `vbmc_pip_package` | `git+https://github.com/openstack/virtualbmc.git@master` |

Example (same host for both protocols):

```bash
# Redfish
curl -sk -u admin:changeme https://192.168.100.50/redfish/v1/Systems/

# IPMI SOL (disconnect virsh console first; exit with Enter then ~.)
ipmitool -I lanplus -C 3 -H 192.168.100.50 -p 623 -U admin -P changeme sol activate
```

Your other playbook can treat `bmc_ip` like an iDRAC address: Redfish base `https://<bmc_ip>/` and IPMI `<bmc_ip>:623`.

### Corporate proxy and PKI (`setup-proxy.yml`)

Simulates a corporate HTTP/HTTPS proxy with Squid SSL bump (MITM). Generates a two-tier PKI:

| Component | Path / artifact | Purpose |
|-----------|-----------------|---------|
| Root CA | `/etc/pki/mirror-operator/root/ca.crt` | Signs dynamic certs for SSL bump; trusted by clients |
| Intermediate CA | `/etc/pki/mirror-operator/intermediate/ca.crt` + `ca.key` | Issuer for cert-manager |
| Squid proxy | `http://proxy.example.com:3128` | HTTP/HTTPS forward proxy on isolated gateway (`192.168.100.1`) |

| Variable | Default | Notes |
|----------|---------|-------|
| `proxy_enabled` | `true` | Set `false` to skip proxy/PKI setup |
| `proxy_listen_port` | `3128` | Squid listen port |
| `proxy_hostname` | `proxy.example.com` | DNS name (resolved via host dnsmasq) |
| `proxy_no_proxy` | localhost, isolated subnet, `.example.com` | `NO_PROXY` list |
| `pki_root_ca_common_name` | `Mirror Operator Root CA` | Root CA subject |
| `pki_intermediate_ca_common_name` | `Mirror Operator Intermediate CA` | Intermediate CA subject |
| `cert_manager_issuer_name` | `mirror-operator-intermediate-ca` | ClusterIssuer name |

**Exported artifacts** (gitignored under `certs/`):

- `root-ca.crt` — install on any client that uses the proxy
- `intermediate-ca.crt`, `intermediate-ca.key`, `intermediate-ca-chain.crt`
- `cluster-proxy-url` — hypervisor public URL for OpenShift cluster-wide proxy config
- `cert-manager-intermediate-ca.yaml` — manual Secret + ClusterIssuer manifest

**OpenShift cluster-wide proxy and root CA trust** (enabled by default):

When `ocp_configure_cluster_proxy` is true (default), the playbooks enforce this safe order:

1. **`setup-proxy.yml`** — PKI + Squid in **forward-proxy mode** (MITM deferred)
2. **`configure-cluster-proxy.yml`** — merge lab + ingress CAs into `openshift-config/user-ca-bundle`, wire `spec.trustedCA.name`, wait for MCP rollout, then set proxy URLs
3. **`enable-proxy-mitm.yml`** — re-enable Squid SSL bump only after nodes trust the lab root CA

The cluster `user-ca-bundle` ConfigMap (not the OLM `cluster-ca-bundle` alone) is what `spec.proxy.trustedCA` rolls out to nodes. By default the playbook merges:

- **Mirror Operator root CA** (MITM / Quay-style trust)
- **Intermediate CA** exported for cert-manager (`intermediate-ca.crt`)
- **Ingress router CA** (`openshift-ingress-operator/router-ca`) so workloads trust default `*.apps` route certificates (e.g. Keycloak)

Operator subscriptions that mount only `cluster-ca-bundle` for `SSL_CERT_FILE` may still need a separate mount for the ingress CA; node-wide trust comes from `user-ca-bundle` after MCP rollout.

Set `-e ocp_configure_cluster_proxy=false` to skip cluster Proxy configuration entirely.

The cluster proxy playbook refuses to run if:

- `certs/cluster-proxy-url` or `ocp_cluster_proxy_url` references `localhost`
- The rendered trust bundle ConfigMap lacks a PEM certificate
- `spec.trustedCA.name` is not wired to the ConfigMap after phase 1
- The hypervisor proxy is unreachable from the Ansible controller

When `ocp_cluster_no_proxy_auto_detect` is true (default), the playbook also adds cluster-local bypass entries such as `.<baseDomain>`, `.<apps-domain>`, the internal API hostname, node `/16` CIDRs, `169.254.169.254`, and `.<region>.compute.internal` on AWS — so internal routes like Quay `.apps.*` do not hairpin through the MITM proxy.

| Variable | Default | Notes |
|----------|---------|-------|
| `ocp_configure_cluster_proxy` | `true` | Set `false` to skip cluster Proxy configuration |
| `proxy_ssl_bump_enabled` | auto | set `false` in `setup-proxy.yml` when cluster proxy is enabled; forced `true` in `enable-proxy-mitm.yml` |
| `ocp_cluster_proxy_url` | `""` | Override auto-detected URL from `certs/cluster-proxy-url` |
| `ocp_cluster_no_proxy_extra` | `""` | Extra hosts/CIDRs appended to cluster `noProxy` |
| `ocp_cluster_no_proxy_auto_detect` | `true` | Append cluster base domain, ingress domain, internal API, node /16 CIDRs, and cloud metadata to `noProxy` |

**Automated ClusterIssuer configuration** (enabled by default):

Step 12 in [`site.yml`](site.yml) applies the intermediate CA to cert-manager. Requires cert-manager Operator installed on the cluster and a valid kubeconfig (`KUBECONFIG` or `~/.kube/config`).

To refresh **only** the OpenShift steps (after the hypervisor lab and `certs/` export already exist):

```bash
ansible-playbook playbooks/openshift/site.yml
```

| Variable | Default | Notes |
|----------|---------|-------|
| `cert_manager_configure_issuer` | `true` | Set `false` to skip Secret + ClusterIssuer creation |
| `cert_manager_namespace` | `cert-manager` | Namespace for the CA Secret |
| `cert_manager_intermediate_secret_name` | `mirror-operator-intermediate-ca` | TLS Secret name referenced by ClusterIssuer |

Manual alternative:

```bash
oc apply -f certs/cert-manager-intermediate-ca.yaml
```

Issue certificates with:

```yaml
spec:
  issuerRef:
    name: mirror-operator-intermediate-ca
    kind: ClusterIssuer
```

The RHEL mirror workstation kickstart trusts the root CA and sets proxy environment variables automatically when `proxy_enabled` is true.

## Input summary

**Must provide:**

- AWS credentials and region
- `rhel_iso_url`

**Should override (security):**

- `vnc_password`
- `rhel_vm_password`
- `rhel_vm_root_password`

**Optional tuning:**

- All other variables in `group_vars/all.yml` have defaults for EC2 sizing, network CIDR/DNS, VM sizing, and Redfish endpoints

**Implicit / environmental:**

- Ansible inventory host (static or EC2-discovered)
- SSH key path (`keys/mirror-operator-key.pem`)

**Runtime outputs (not inputs):**

- `ec2_ami`, `ec2_vpc_id`, `ec2_subnet_id`, `sno_vm_uuid`, dynamically added hypervisor IPs

OpenShift/ODF tuning (`ocp_kubeconfig`, `gp3_high_perf_*`, `odf_*`, `noobaa_*`, MachineSet replicas) lives in [`group_vars/all.yml`](group_vars/all.yml) and is used by steps 10–17 above.

**Prerequisites for OpenShift steps:** `oc` CLI, reachable cluster API, PKI under `certs/` from `setup-proxy.yml`, and cert-manager Operator for the ClusterIssuer step. Use `-e ocp_configure_cluster_proxy=false` or `-e cert_manager_configure_issuer=false` to skip individual playbooks inside the stack.
