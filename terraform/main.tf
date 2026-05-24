terraform {
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.49"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
  required_version = ">= 1.6"
}

# --- Inputs -------------------------------------------------------------------

variable "hcloud_token" {
  type      = string
  sensitive = true
}

variable "github_token" {
  type        = string
  sensitive   = true
  description = "PAT with read:packages (ghcr pull) + repo (private-repo clone)."
}

variable "github_username" {
  type = string
}

variable "github_repo" {
  type        = string
  description = "owner/repo of the application repo, e.g. kjell/007. Cloned to /opt/agent-007 at first boot."
}

variable "ssh_key_name" {
  type        = string
  description = "Name of an SSH key already uploaded to Hetzner Cloud."
}

variable "cloudflare_api_token" {
  type        = string
  sensitive   = true
  description = "Cloudflare API token scoped to Zone:DNS:Edit on the target zone."
}

variable "cloudflare_zone_id" {
  type        = string
  description = "Cloudflare zone ID that owns app_domain."
}

variable "app_domain" {
  type        = string
  description = "FQDN to point at the server (e.g. task.example.com). Also set APP_DOMAIN in the app .env so Caddy provisions the cert for it."
}

# --- Provider -----------------------------------------------------------------

provider "hcloud" {
  token = var.hcloud_token
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

data "hcloud_ssh_key" "existing_key" {
  name = var.ssh_key_name
}

# --- Firewall -----------------------------------------------------------------

# Inbound: SSH for admin, 80/443 for Caddy, ICMP for ping. Everything else is
# blocked at Hetzner's edge — the host's Postgres/Redis ports stay private
# regardless of how docker-compose binds them.
resource "hcloud_firewall" "web" {
  name = "agent-007-web"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

# --- Server -------------------------------------------------------------------

# user_data carries only the bootstrap tokens (which are needed to clone the
# repo and pull from ghcr) — never the app's .env, docker-compose.yml, or
# Caddyfile. Those live in the repo and get refreshed by `git pull` on the
# host, so updating them no longer requires a terraform apply.
resource "hcloud_server" "web" {
  name         = "agent-007"
  server_type  = "cx23"
  image        = "ubuntu-24.04"
  location     = "nbg1"
  ssh_keys     = [data.hcloud_ssh_key.existing_key.id]
  firewall_ids = [hcloud_firewall.web.id]

  user_data = templatefile("${path.module}/cloud-config.yml", {
    github_token    = var.github_token
    github_username = var.github_username
    github_repo     = var.github_repo
  })

  public_net {
    ipv4_enabled = true
  }
}

# --- DNS ---------------------------------------------------------------------

# Manage the public A record alongside the server. Terraform updates it
# automatically if the server is recreated and lands on a new IP.
resource "cloudflare_record" "app" {
  zone_id         = var.cloudflare_zone_id
  name            = var.app_domain
  content         = hcloud_server.web.ipv4_address
  type            = "A"
  ttl             = 300
  proxied         = false
  allow_overwrite = true
}

# --- Outputs ------------------------------------------------------------------

output "server_ip" {
  value = hcloud_server.web.ipv4_address
}

output "next_steps" {
  value = <<-EOT
    Server is up at ${hcloud_server.web.ipv4_address}.

    Finish setup (the VM has no .env yet — it cannot start the app):
      scp .env root@${hcloud_server.web.ipv4_address}:/opt/agent-007/.env
      ssh root@${hcloud_server.web.ipv4_address} 'cd /opt/agent-007 && docker compose pull && docker compose up -d'
  EOT
}
