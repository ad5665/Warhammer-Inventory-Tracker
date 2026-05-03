# AWS Lightsail Terraform

This Terraform stack runs the existing Docker image on one Amazon Lightsail instance with:

- a Lightsail instance using the low-cost `micro_3_0` bundle by default
- a Lightsail static IPv4 address
- Lightsail firewall rules for HTTP, HTTPS, and SSH
- an imported Lightsail SSH key pair from your local public key
- Docker Compose running the app plus Caddy as the reverse proxy
- local persistent app data under `/opt/wh40k-stock-tracker/data`
- nightly local backup archives under `/opt/wh40k-stock-tracker/backups`
- optional Lightsail automatic snapshots
- optional Route 53 `A` record for a custom hostname

Lightsail does not use EC2 security groups. Its equivalent here is `aws_lightsail_instance_public_ports`.
The default networking mode is IPv4 because this stack attaches a static IPv4 address for stable DNS.

## Prerequisites

- Terraform 1.6+
- AWS credentials configured for the target account
- a local SSH public key, for example `~/.ssh/id_ed25519.pub`
- an already-published app image, defaulting to `ad5665/warhammer-inventory-tracker:latest`

Check current Lightsail bundle and blueprint values if AWS rejects the defaults:

```bash
aws lightsail get-bundles --region eu-west-2
aws lightsail get-blueprints --region eu-west-2
```

## Deploy

```bash
cd infra/lightsail
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`, especially:

- `ssh_public_key_path`
- `ssh_cidrs`
- `domain_name`, if you have one
- `route53_zone_id`, if Route 53 should manage the DNS record
- `enable_auto_snapshot`, if you want off-instance recovery points

Then run:

```bash
terraform init
terraform plan
terraform apply
```

Terraform outputs the static IP, app URL, SSH command, and a command for reading the initial admin password from the container logs.

## DNS and HTTPS

When `domain_name` is set, Caddy serves that hostname and requests a Let's Encrypt certificate. Either:

- set `route53_zone_id` so Terraform creates the `A` record, or
- manually point your DNS `A` record at the Terraform `static_ip` output.

When `domain_name` is empty, Caddy serves plain HTTP on the static IP and `WH40K_COOKIE_SECURE` defaults to `false`.

## Operations

View app logs:

```bash
ssh ubuntu@<static-ip> 'sudo docker logs wh40k-stock-tracker'
```

Update to the latest container image:

```bash
ssh ubuntu@<static-ip> 'cd /opt/wh40k-stock-tracker && sudo docker compose pull && sudo docker compose up -d'
```

Run a manual local backup:

```bash
ssh ubuntu@<static-ip> 'sudo /usr/local/bin/wh40k-backup'
```

The local backup protects against application-level mistakes, but it is still on the same instance disk. Use Lightsail snapshots for disaster recovery before major changes.

Set `enable_auto_snapshot = true` for daily Lightsail snapshots. Snapshot storage is billed separately, so it is off by default in this cost-focused stack. Lightsail snapshot times must be hourly UTC values like `04:00`.

## Data Warning

The SQLite database and uploads live on the Lightsail instance root disk. `terraform destroy` destroys that data unless you have copied it elsewhere or restored from a Lightsail snapshot.
