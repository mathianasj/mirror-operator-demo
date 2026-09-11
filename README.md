# Mirror Operator Demo

Ansible automation for a KVM hypervisor lab on AWS bare metal. The top-level playbook provisions an EC2 host, configures KVM/GNOME/VNC, creates an isolated libvirt network, and deploys a RHEL 9 mirror workstation plus demo VMs with Redfish simulation.

A separate OpenShift playbook stack (`playbooks/openshift/site.yml`) handles post-install ODF/NooBaa configuration and is not invoked by the root `site.yml`.

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

[`site.yml`](site.yml) imports eleven child playbooks in order:

1. [`playbooks/provision-ec2.yml`](playbooks/provision-ec2.yml) — launch bare-metal EC2 instance
2. [`playbooks/configure-host.yml`](playbooks/configure-host.yml) — KVM, GNOME, VNC, Cockpit, NTP, DNS
3. [`playbooks/setup-proxy.yml`](playbooks/setup-proxy.yml) — corporate MITM proxy (Squid SSL bump) and PKI
4. [`playbooks/create-networks.yml`](playbooks/create-networks.yml) — default NAT + isolated libvirt network
5. [`playbooks/create-storage.yml`](playbooks/create-storage.yml) — libvirt storage pool and ISO directory
6. [`playbooks/create-fedora-vm.yml`](playbooks/create-fedora-vm.yml) — RHEL 9 mirror workstation (FIPS + DISA STIG)
7. [`playbooks/create-bare-vm.yml`](playbooks/create-bare-vm.yml) — bare VM with extra disk
8. [`playbooks/create-sno-vm.yml`](playbooks/create-sno-vm.yml) — SNO demo VM
9. [`playbooks/setup-redfish.yml`](playbooks/setup-redfish.yml) — sushy Redfish emulator
10. [`playbooks/openshift/configure-cluster-proxy.yml`](playbooks/openshift/configure-cluster-proxy.yml) — cluster trust bundle + proxy URLs, then [`enable-proxy-mitm.yml`](playbooks/enable-proxy-mitm.yml) (enabled by default)
11. [`playbooks/openshift/configure-cert-manager-issuer.yml`](playbooks/openshift/configure-cert-manager-issuer.yml) — cert-manager ClusterIssuer (enabled by default)

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
- **Re-run child playbooks only** (skip `provision-ec2.yml`): temporarily add the current public IP to `inventory/hosts.yml`, or use `--limit <current-ip>`.

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

**Runtime-derived (not user inputs):** `ec2_ami`, `ec2_ami_name` (latest RHEL 9 Hourly GP3 AMI), `ec2_vpc_id`, `ec2_subnet_id`, `ec2_instance_names`.

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

### Redfish / sushy-emulator (`setup-redfish.yml`)

| Variable | Default |
|----------|---------|
| `sushy_emulator_listen_ip` | `192.168.100.1` |
| `sushy_emulator_port` | `8000` |
| `sushy_emulator_libvirt_uri` | `qemu:///system` |
| `sushy_emulator_ssl_cert` | `/etc/sushy/tls/cert.pem` |
| `sushy_emulator_ssl_key` | `/etc/sushy/tls/key.pem` |

Config from [`templates/sushy-emulator.conf.j2`](templates/sushy-emulator.conf.j2). A self-signed TLS certificate is generated on first run.

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
2. **`configure-cluster-proxy.yml`** — apply root CA ConfigMap + `spec.trustedCA.name`, wait for worker MCP rollout, then set proxy URLs
3. **`enable-proxy-mitm.yml`** — re-enable Squid SSL bump only after nodes trust the lab root CA

Set `-e ocp_configure_cluster_proxy=false` to skip cluster Proxy configuration entirely.

The cluster proxy playbook refuses to run if:

- `certs/cluster-proxy-url` or `ocp_cluster_proxy_url` references `localhost`
- The rendered trust bundle ConfigMap lacks a PEM certificate
- `spec.trustedCA.name` is not wired to the ConfigMap after phase 1
- The hypervisor proxy is unreachable from the Ansible controller

| Variable | Default | Notes |
|----------|---------|-------|
| `ocp_configure_cluster_proxy` | `true` | Set `false` to skip cluster Proxy configuration |
| `proxy_ssl_bump_enabled` | auto | set `false` in `setup-proxy.yml` when cluster proxy is enabled; forced `true` in `enable-proxy-mitm.yml` |
| `ocp_cluster_proxy_url` | `""` | Override auto-detected URL from `certs/cluster-proxy-url` |
| `ocp_cluster_no_proxy_extra` | `""` | Extra hosts/CIDRs appended to cluster `noProxy` |

**Automated ClusterIssuer configuration** (enabled by default):

Step 11 in [`site.yml`](site.yml) applies the intermediate CA to cert-manager. Requires cert-manager Operator installed on the cluster and a valid kubeconfig:

```bash
ansible-playbook site.yml \
  -e vnc_password="..." \
  ...
```

Or via the OpenShift stack (runs after `connect-cluster.yml`):

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

## Variables not used by `site.yml`

[`group_vars/all.yml`](group_vars/all.yml) also defines OpenShift/ODF variables (`ocp_kubeconfig`, `gp3_high_perf_*`, `odf_*`, `noobaa_*`). These are consumed only by [`playbooks/openshift/site.yml`](playbooks/openshift/site.yml) and are not inputs to the hypervisor lab playbook.

## OpenShift stack (separate)

To configure OpenShift post-install (cluster proxy/CA trust, ClusterIssuer, worker scaling, gp3-high-perf StorageClass, standalone NooBaa/ODF):

```bash
ansible-playbook playbooks/openshift/site.yml
```

Requires `oc` CLI, a valid kubeconfig (`KUBECONFIG` or `~/.kube/config`), and cert-manager Operator for step 11. Cluster proxy (step 10) is on by default — set `-e ocp_configure_cluster_proxy=false` to skip. Set `-e cert_manager_configure_issuer=false` to skip the ClusterIssuer step. PKI artifacts must exist in `certs/` (exported by `setup-proxy.yml` during the hypervisor run). See `group_vars/all.yml` for OpenShift/ODF variable defaults.
